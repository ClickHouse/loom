"""Bounded machine translation of tracked README.md and docs/**/*.md.

    python -m scripts.i18n.translate check   # strict missing/stale/obsolete coverage
    python -m scripts.i18n.translate check --skip-stale  # validate present output
    python -m scripts.i18n.translate update  # bounded batch; needs paid-call authorization

The main-only workflow books a durable cumulative allowance before calling Bedrock.
The generator shares one approximate actual-usage cap across every page and language.
Requests retain complete Markdown units within a 16 KiB source-byte bound. The assembled
page keeps the source's invariants and table layout before it is staged. Relative links
use available translated targets and otherwise point to English. Obsolete generated
output waits for surviving inbound links.
"""

from __future__ import annotations

import argparse
import math
import os
import posixpath
import re
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from scripts.i18n import chunks, context, lifecycle, refresh
from scripts.i18n.chunks import ANCHOR, CODE_SPAN, HTML_TAG, outside_fences

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = "i18n"
REPO_URL = "https://github.com/ClickHouse/loom"
ROLLOUT_LANGUAGES = ("zh-CN", "ja", "ko", "pt-BR", "es")


def sources() -> tuple[str, ...]:
    """Tracked English Markdown that still exists in the working tree."""
    names = subprocess.run(
        ["git", "ls-files", "-z", "--", "README.md", "docs"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    found = []
    for name in sorted(set(names)):
        if name != "README.md" and not (name.startswith("docs/") and name.endswith(".md")):
            continue
        if not lifecycle.source_path(name):
            raise SystemExit(f"unsupported English source path: {name!r}")
        file = ROOT / name
        if any(parent.is_symlink() for parent in (file, *file.parents)):
            raise SystemExit(f"English source is not a regular file: {name}")
        if file.exists():
            if not lifecycle.regular(name):
                raise SystemExit(f"English source is not a regular file: {name}")
            found.append(name)
    return tuple(found)


@dataclass(frozen=True)
class Language:
    code: str
    name: str  # the language's own name, shown in the language line
    english: str  # the name the prompt uses
    banner: str  # "{url}" is the English page
    style: str  # punctuation and fixed terms, so sections translated apart read alike


LANGUAGES = (
    Language(
        "zh-CN",
        "简体中文",
        "Simplified Chinese",
        "本页由机器从[英文原文]({url})翻译，内容以英文原文为准。",
        "Use full-width Chinese punctuation (，。：；（）) in prose, and join the lines of a "
        "paragraph into one line. Fixed terms: dashboard 控制台, "
        "compiler tier 编译器层级, fast tier 快速层级, an access token 令牌 (model "
        "tokens stay token), namespace 命名空间, memory 记忆; keep agent as agent.",
    ),
    Language(
        "ja",
        "日本語",
        "Japanese",
        "このページは[英語の原文]({url})の機械翻訳です。内容は英語の原文が優先されます。",
        "Use Japanese punctuation (、。：（）) in prose, and join the lines of a paragraph "
        "into one line. Fixed terms: dashboard ダッシュボード, "
        "compiler tier コンパイラティア, fast tier 高速ティア, the NOTICE file NOTICE (a legal "
        "notice; keep the name), token トークン, namespace "
        "名前空間, memory メモリ, agent エージェント.",
    ),
    Language(
        "ko",
        "한국어",
        "Korean",
        "이 페이지는 [영어 원문]({url})을 기계 번역한 것이며, 영어 원문이 기준입니다.",
        "Fixed terms: dashboard 대시보드, compiler tier 컴파일러 티어, fast tier 빠른 티어, "
        "recording (of conversations) 기록, "
        "token 토큰, namespace 네임스페이스, memory 메모리, agent 에이전트.",
    ),
    Language(
        "pt-BR",
        "Português (Brasil)",
        "Brazilian Portuguese",
        "Esta página é uma tradução automática da [página em inglês]({url}), que prevalece.",
        "Fixed terms: dashboard painel, compiler tier camada do compilador, fast tier camada "
        "rápida, token token, namespace namespace, memory memória, agent agente.",
    ),
    Language(
        "es",
        "Español",
        "Spanish",
        "Esta página es una traducción automática de la [página en inglés]({url}), que prevalece.",
        "Fixed terms: dashboard panel, compiler tier nivel del compilador, fast tier nivel "
        "rápido, override sobrescritura, token token, namespace espacio de nombres, memory "
        "memoria, agent agente.",
    ),
)

# USD per million tokens (input, output) at Anthropic's list prices. Bedrock bills at its own
# prices, so the cap is approximate. A model missing here is refused: the cap needs a price.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
# The runner's instance role may call this model only (CONTRIBUTING.md#translations): another
# model needs that role's policy changed first.
DEFAULT_MODEL = "claude-sonnet-5-5"
# Bedrock's global inference profile of a model; the stamp keeps the plain name.
BEDROCK_MODEL_PREFIX = "global.anthropic."
MAX_TOKENS = 32000

LANGUAGE_LINE = re.compile(r"<!-- languages -->.*?<!-- /languages -->", re.DOTALL)
STAMP = re.compile(r"\A<!-- i18n: source=(.*?) blob=([0-9a-f]{40}) model=(\S+) -->\n")
# A Markdown link or image target, an HTML href or src, or a reference definition.
LINK = re.compile(r"(\]\(|(?:href|src)=\"|^\[(?!\^)[^\]\n]+\]:[ \t]+)([^)\"\s]+)", re.MULTILINE)
# A Markdown link's text (not an image's).
LINK_TEXT = re.compile(r"(?<!!)\[([^\[\]\n]+)\]\([^)\s]+")
# An HTML link's inner text; `<code>` inside it is dropped with its content, other tags
# (`<b>`) without theirs, before checking.
HTML_LINK_TEXT = re.compile(r"<a\s[^>]*>(.*?)</a>", re.DOTALL)
HTML_CODE = re.compile(r"<code>.*?</code>", re.DOTALL)
IMAGE_ALT = re.compile(r"!\[([^\]\n]+)\]\(")
HTML_ALT = re.compile(r'\balt="([^"]+)"')
# Words a label (heading, link text, alt text, table cell) may keep in every language:
# names, and the two terms some glossaries keep as they are.
KEPT_WORDS = frozenset(
    """loom clickhouse postgres claude code codex cursor docker compose mcp sdk sdks python
    typescript node langgraph strands kubernetes helm github codespaces http https api cli
    okf rag llm json yaml sql oidc scim tls otel clickstack hyperdx langfuse gemini vs windsurf zed
    cline pypi npm apache ci url id token namespace go rust java javascript c""".split()
)
URL = re.compile(r"https?://[^\s<>()\"'`*\]]+")
HEADING = re.compile(r"^(#{1,6}) +(.+?) *$")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

SYSTEM = """You translate a Markdown page of Loom's documentation, or one section of it, into
{language}.
Loom is an open-source memory, document search and code index server for AI agents.
Rules:
- Translate the prose, headings, table cells, list items, image alt text and every link
  text, including links to English pages; only names stay in English.
- Copy byte for byte: fenced code blocks, inline code spans, URLs and link targets, HTML
  tags and attributes, lines of the form <a id="..."></a>, and every number.
- Keep product, company and project names, CLI commands, flags, file names and op names
  in English (Loom, ClickHouse, Postgres, Claude Code, Codex, Docker, MCP, SDK).
- Keep the Markdown structure: the same headings in the same order, the same list and
  table layout, the same blank lines between blocks.
- Write the plain, technical register of developer documentation in {language}.
- {style}
Reply with the translated text only, with no preamble and no code fence around it."""


# ---- page structure -------------------------------------------------------------------


def slug(heading: str, seen: Counter[str]) -> str:
    """The anchor `tests/unit/markdown_links.py` checks the English pages' links against:
    tags dropped, lowercase, punctuation dropped, spaces to hyphens, a repeated anchor
    numbered from -1."""
    text = re.sub(r"<[^>]+>", "", heading).strip().lower()
    base = re.sub(r"[^\w\- ]", "", text).replace(" ", "-")
    n = seen[base]
    seen[base] += 1
    return base if n == 0 else f"{base}-{n}"


def anchor_headings(text: str, seen: Counter[str]) -> str:
    """Put `<a id="english-anchor"></a>` above every heading outside code; `seen` carries
    the page's anchors across its sections."""
    out: list[str] = []
    for line, in_code in outside_fences(text):
        heading = None if in_code else HEADING.match(line)
        if heading:
            out += [f'<a id="{slug(heading.group(2), seen)}"></a>', ""]
        out.append(line)
    return "\n".join(out)


# ---- links ----------------------------------------------------------------------------


def target_path(lang: str, source: str) -> str:
    return f"{OUT_DIR}/{lang}/{source}"


def in_prose(text: str, change: Callable[[str], str]) -> str:
    """`text` with `change` applied to its prose only: not to fenced code, not to code spans."""
    out: list[str] = []
    chunk: list[str] = []

    def flush() -> None:
        prose = "\n".join(chunk)
        parts, at = [], 0
        for span in CODE_SPAN.finditer(prose):
            parts += [change(prose[at : span.start()]), span.group(0)]
            at = span.end()
        out.append("".join([*parts, change(prose[at:])]))
        chunk.clear()

    for line, in_code in outside_fences(text):
        if in_code:
            if chunk:
                flush()
            out.append(line)
        else:
            chunk.append(line)
    if chunk:
        flush()
    return "\n".join(out)


def rewrite_links(text: str, source: str, lang: str) -> str:
    """Point every relative link of `source` at its target as seen from the translation."""
    here = posixpath.dirname(target_path(lang, source))
    inventory = frozenset(sources())

    def fix(prose: str, match: re.Match[str]) -> str:
        prefix, link = match.groups()
        if re.match(r"[a-z]+:|#|/", link):
            return match.group(0)
        path, _, anchor = link.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(path)))
        suffix = f"#{anchor}" if anchor else ""
        translated = ROOT / target_path(lang, resolved)
        if (
            resolved in inventory
            and translated.is_file()
            and not translated.is_symlink()
            and (not anchor or f'<a id="{unquote(anchor)}"></a>' in translated.read_text())
        ):
            return prefix + quote(posixpath.relpath(target_path(lang, resolved), here)) + suffix
        image = prefix == 'src="' or (prefix == "](" and _is_image(prose, match.start()))
        return f"{prefix}{REPO_URL}/{'raw' if image else 'blob'}/main/{quote(resolved)}{suffix}"

    return in_prose(text, lambda prose: LINK.sub(lambda m: fix(prose, m), prose))


def _is_image(text: str, close: int) -> bool:
    """Whether the Markdown link whose text ends at `close` (its `]`) is an image: the
    matching `[` follows a `!`."""
    depth = 0
    for i in range(close - 1, -1, -1):
        if text[i] == "]":
            depth += 1
        elif text[i] == "[":
            if depth == 0:
                return i > 0 and text[i - 1] == "!"
            depth -= 1
    return False


def language_line(current: str, *, available: set[str] | None = None) -> str:
    """The README's language links as seen from `current` (a path relative to the root)."""
    present = lifecycle.regular if available is None else available.__contains__
    here = posixpath.dirname(current)
    items = [("README.md", "English")] + [
        (target_path(lg.code, "README.md"), lg.name) for lg in LANGUAGES
    ]
    links = [
        name
        if path == current or not present(path)
        else f'<a href="{posixpath.relpath(path, here) if here else "./" + path}">{name}</a>'
        for path, name in items
    ]
    docs = readme_documentation(current)
    return "<!-- languages -->\n  " + " · ".join(links) + docs + "\n  <!-- /languages -->"


def readme_documentation(current: str) -> str:
    """Link a translated README to an existing documentation landing in its language."""
    from scripts.docs_site import indexnow
    from scripts.docs_site.messages import COPY
    from scripts.docs_site.paths import landing_sources

    language = next((lg for lg in LANGUAGES if current == target_path(lg.code, "README.md")), None)
    if language is None:
        return ""
    prefix = f"{OUT_DIR}/{language.code}/docs/"
    available = {
        path.removeprefix(prefix): path
        for path in lifecycle.output_files()
        if path.startswith(prefix) and path.endswith(".md") and lifecycle.regular(path)
    }
    for source in landing_sources(available):
        target = available[source]
        if lifecycle.owner(target, (ROOT / target).read_text()) is not None:
            url = indexnow.SITE + language.code + "/" + quote(indexnow.page_path("docs/" + source))
            return f'\n  <br>\n  <a href="{url}">{COPY[language.code]["documentation"]}</a>'
    return ""


# ---- verification ---------------------------------------------------------------------


def code_blocks(text: str) -> list[str]:
    """The fenced code blocks of `text`, in order."""
    blocks: list[list[str]] = []
    inside = False
    for line, in_code in outside_fences(text):
        if in_code and not inside:
            blocks.append([])
        if in_code:
            blocks[-1].append(line)
        inside = in_code
    return ["\n".join(b) for b in blocks]


def invariants(text: str) -> dict[str, Counter[str]]:
    """What a translation must carry over unchanged, besides its code blocks."""
    prose = [line for line, in_code in outside_fences(text) if not in_code]
    body = "\n".join(prose)
    without_spans = CODE_SPAN.sub(" ", body)
    without_urls = URL.sub(" ", LINK.sub(" ", without_spans))
    return {
        "inline code": Counter(" ".join(m.group(2).split()) for m in CODE_SPAN.finditer(body)),
        "link targets": Counter(m.group(2) for m in LINK.finditer(without_spans)),
        "URLs": Counter(URL.findall(without_spans)),
        "anchors": Counter(m.group(1) for m in map(ANCHOR.match, prose) if m),
        "headings": Counter(m.group(1) for m in map(HEADING.match, prose) if m),
        "numbers": Counter(NUMBER.findall(without_urls)),
        "HTML tags": Counter(
            HTML_ALT.sub('alt="translated"', tag) for tag in HTML_TAG.findall(without_spans)
        ),
    }


def _words(label: str) -> list[str]:
    """The words of `label` that are not names, outside link targets."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", LINK.sub(" ", label))
    return [w for w in words if w.lower() not in KEPT_WORDS]


def _label_links(text: str) -> tuple[tuple[str, str], ...]:
    return tuple((m[1], m[0].partition("](")[2]) for m in LINK_TEXT.finditer(text))


def _label_nodes(text: str) -> dict[tuple, context.Label]:
    # Retain character offsets so a request can be checked inside its source containers.
    def blank(value: str) -> str:
        return "".join("\n" if c == "\n" else " " for c in value)

    prose = "\n".join(blank(line) if code else line for line, code in outside_fences(text))
    prose = CODE_SPAN.sub(lambda m: blank(m[0]), prose)
    nodes = {}
    offset = 0
    heading_index = 0
    for line in prose.splitlines(keepends=True):
        if heading := HEADING.match(line.rstrip("\n")):
            nodes[("heading", heading_index)] = context.Label(
                HTML_TAG.sub(" ", heading[2]), start=offset, end=offset + len(line.rstrip("\n"))
            )
            heading_index += 1
        offset += len(line)
    seen: Counter[tuple] = Counter()
    for kind, regex in (
        ("link", LINK_TEXT),
        ("html", HTML_LINK_TEXT),
        ("alt", IMAGE_ALT),
        ("html_alt", HTML_ALT),
    ):
        for match in regex.finditer(prose):
            inner = match[1]
            links = _label_links(match[0]) if kind == "link" else ()
            if kind == "html":
                target = LINK.search(match[0].split(">", 1)[0])
                inner = HTML_TAG.sub(" ", HTML_CODE.sub(" ", inner)).strip()
                links = ((inner, target[2]),) if target else ()
            identity = (kind, links[0][1] if links else "")
            key = (*identity, seen[identity])
            seen[identity] += 1
            nodes[key] = context.Label(inner, links=links, start=match.start(), end=match.end())
    nodes.update(chunks.table_labels(text))
    return nodes


def _residual_label(node: context.Label, atoms: tuple[str, ...]) -> str:
    residual, _spelling = context.project(node, atoms)
    # A name plus one ordinary qualifier still has a translation obligation.
    minimum = 1 if atoms else node.minimum
    return " ".join(residual.split()) if len(_words(residual)) >= minimum else ""


def _source_protections(
    nodes: dict[tuple, context.Label], source_path: str
) -> dict[tuple, tuple[str, ...]]:
    # Only tracked repository targets attest filenames; a remote basename proves nothing.
    files = frozenset()
    if any(node.links for node in nodes.values()) and (ROOT / ".git").exists():
        paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode()
        files = frozenset(paths.rstrip("\0").split("\0"))
    return {
        key: context.protected(node, source_path, files=files, repo_url=REPO_URL)
        for key, node in nodes.items()
    }


def labels(text: str, *, source_path: str = "") -> Counter[str]:
    """Translatable labels after protecting only source-attested spelling spans."""
    nodes = _label_nodes(text)
    protections = _source_protections(nodes, source_path)
    return Counter(
        label for key, node in nodes.items() if (label := _residual_label(node, protections[key]))
    )


def _label_differences(
    source: str,
    translation: str,
    source_path: str,
    scope: context.SourceContext | None = None,
) -> list[str]:
    english = scope.english if scope else source
    if scope and english[scope.start : scope.end] != source:
        raise ValueError("translation request does not match its source context")
    candidate = scope.candidate(translation) if scope else translation
    start, end = (scope.start, scope.end) if scope else (0, len(source))
    target_end = start + len(translation)
    wanted, actual = _label_nodes(english), _label_nodes(candidate)
    source_labels: Counter[str] = Counter()
    target_labels: Counter[str] = Counter()
    protections = _source_protections(wanted, source_path)
    problems = []
    for key, node in wanted.items():
        atoms = protections[key]
        if not (start <= node.start and node.end <= end):
            continue
        if label := _residual_label(node, atoms):
            source_labels[label] += 1
        if atoms:
            translated = actual.get(key, context.Label(""))
            expected = context.project(node, atoms)[1]
            got = context.project(translated, atoms)[1]
            if (
                expected != got
                or node.field != translated.field
                or node.default_for != translated.default_for
                or node.family_for != translated.family_for
            ):
                problems.append(f"protected source strings {key}: expected {expected}, got {got}")
    for key, node in actual.items():
        if start <= node.start and node.end <= target_end:
            if label := _residual_label(node, protections.get(key, ())):
                target_labels[label] += 1
    if untranslated := source_labels & target_labels:
        problems.insert(
            0,
            "left in English (headings, link texts, alt texts, table cells): "
            + f"{sorted(untranslated)[:8]}",
        )
    return problems


def differences(
    source: str,
    translation: str,
    *,
    source_path: str = "",
    source_context: context.SourceContext | None = None,
) -> list[str]:
    """How `translation` breaks the invariants of `source`; empty when it keeps them.
    A translation may add a number (a spelled-out one written as digits), never drop one,
    and may not hand back a heading, link text, alt text or table cell in English."""
    source_path = source_context.path if source_context else source_path
    found = _label_differences(source, translation, source_path, source_context)
    if code_blocks(source) != code_blocks(translation):
        found.append("code blocks: copy every fenced code block unchanged, in the same order")
    want, got = invariants(source), invariants(translation)
    for name, expected in want.items():
        actual = got[name]
        missing = expected - actual
        extra = Counter() if name == "numbers" else actual - expected
        if missing or extra:
            found.append(f"{name}: missing {sorted(missing)[:8]}, unexpected {sorted(extra)[:8]}")
    return found


# ---- stamps ---------------------------------------------------------------------------


def blob(source: str) -> str:
    return subprocess.run(
        ["git", "hash-object", source], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def header(source: str, lang: Language, model: str, blob_sha: str) -> str:
    """A translation's first lines: the stamp and the two banner lines."""
    url = f"{REPO_URL}/blob/main/{quote(source)}"
    english = f"Machine-translated from the [English page]({url}), which is authoritative."
    stamp = f"<!-- i18n: source={source} blob={blob_sha} model={model} -->\n"
    return f"{stamp}> {lang.banner.format(url=url)}\n> {english}\n\n"


def stale(
    languages: tuple[Language, ...] = LANGUAGES, model: str = DEFAULT_MODEL
) -> list[tuple[str, Language]]:
    """Every (source, language) whose translation is missing, or whose stamp or banner
    differs: another English blob, another model, another banner."""
    out = []
    for source in sources():
        current = blob(source)
        for lang in languages:
            path = ROOT / target_path(lang.code, source)
            text = path.read_text() if path.is_file() else ""
            if not text.startswith(header(source, lang, model, current)):
                out.append((source, lang))
    return out


def orphans() -> list[str]:
    """Every output outside the current source/language inventory, including unknown files."""
    expected = {target_path(lang.code, s) for s in sources() for lang in LANGUAGES}
    return sorted(set(lifecycle.output_files()) - expected)


def prose_links(text: str) -> list[re.Match[str]]:
    """The link matches of `text` outside fenced code and code spans."""
    found: list[re.Match[str]] = []

    def collect(prose: str) -> str:
        found.extend(LINK.finditer(prose))
        return prose

    in_prose(text, collect)
    return found


def link_problems() -> list[str]:
    """Check actual present links and anchors, including stale and obsolete translations."""
    return lifecycle.link_problems()


def untranslated_pages() -> list[str]:
    """Translations that kept a heading, link text, alt text or table cell in English."""
    problems = []
    for source in sources():
        for lang in LANGUAGES:
            path = ROOT / target_path(lang.code, source)
            if not path.is_file():
                continue
            english = "\n".join(english_requests(prepare(source, lang)[0]))
            text = path.read_text()
            # Generated banner links must not shift the source body's node identities.
            if (stamp := STAMP.match(text)) and stamp[1] == source:
                prefix = header(source, lang, stamp[3], stamp[2])
                if text.startswith(prefix):
                    text = text[len(prefix) :]
            rel = path.relative_to(ROOT).as_posix()
            problems.extend(
                f"{rel}: {problem}" for problem in _label_differences(english, text, source)
            )
    return problems


def readmes() -> list[str]:
    """The README and every translated README that exists: the pages with a language line."""
    paths = ["README.md"] + [target_path(lg.code, "README.md") for lg in LANGUAGES]
    return [p for p in paths if (ROOT / p).is_file()]


def language_lines_differ() -> list[str]:
    """The READMEs whose language line is not the generated one, e.g. after a language is
    added: `update` rewrites them without translating anything."""
    out = []
    for path in readmes():
        match = LANGUAGE_LINE.search((ROOT / path).read_text())
        if not match or match.group(0) != language_line(path):
            out.append(path)
    return out


# ---- the API --------------------------------------------------------------------------


class TranslationDiffers(Exception):
    """A page's translation breaks the invariants: a section after its retry, or the
    assembled page (a label borrowed from another section)."""


@dataclass
class Spend:
    """One cumulative usage cap across every selected page and language."""

    model: str
    cap_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    stopped: bool = False

    @property
    def usd(self) -> float:
        price_in, price_out = PRICES[self.model]
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1e6

    def before_call(self) -> None:
        if self.stopped:
            raise SystemExit("stopped: the cap was reached")
        spent = self.usd
        if spent > self.cap_usd:
            self.stopped = True
            raise SystemExit(f"spent ${spent:.2f}, over LOOM_I18N_MAX_USD=${self.cap_usd:.2f}")

    def add(self, usage: dict[str, int]) -> None:
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)


def request_chars(source: str, lang: Language) -> int:
    """The characters one translation of `source` sends: each request with the system prompt."""
    text, _ = prepare(source, lang)
    system = len(SYSTEM.format(language=lang.english, style=lang.style))
    return sum(len(s) + system for s in english_requests(text))


def estimate_usd(model: str, chars: int) -> float:
    """About three characters per input token, and a reply of 1.2 times the input's tokens.
    Measured runs cost from about 20% under the estimate to 6% over it (retries add input
    the estimate does not see). `Spend` is the hard cap; this only refuses a run that cannot fit."""
    price_in, price_out = PRICES[model]
    tokens = chars / 3
    return (tokens * price_in + 1.2 * tokens * price_out) / 1e6


def bedrock_client() -> Any:
    """An AnthropicBedrock client with retries off: `call` decides what to retry."""
    try:
        from anthropic import AnthropicBedrock
    except ImportError as e:
        raise SystemExit(
            "translations need the anthropic package: "
            "pip install --require-hashes --no-deps --only-binary=:all: "
            "-r scripts/i18n/requirements.txt"
        ) from e
    try:
        return AnthropicBedrock(max_retries=0)
    except ValueError as e:  # no region could be resolved
        raise SystemExit(f"AnthropicBedrock: {e}; set AWS_REGION") from e


def call(client: Any, model: str, system: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    import anthropic

    for attempt in range(4):
        try:
            # An explicit timeout lets a 32k-token reply go without streaming.
            reply = client.messages.create(
                model=BEDROCK_MODEL_PREFIX + model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                output_config={"effort": "low"},
                # InvokeModel uses Bedrock's request version.
                extra_body={"anthropic_version": "bedrock-2023-05-31"},
                timeout=600,
            )
            return reply.to_dict()
        except anthropic.APITimeoutError as e:
            # The request was sent and may be billed; a retry would spend unseen tokens.
            raise SystemExit(f"Bedrock timed out: {e}") from e
        except anthropic.APIStatusError as e:
            if e.status_code not in (429, 500, 502, 503, 529) or attempt == 3:
                raise SystemExit(f"Bedrock {e.status_code}: {str(e)[:500]}") from e
        except anthropic.APIConnectionError as e:
            if attempt == 3:
                raise SystemExit(f"Bedrock unreachable: {e}") from e
        time.sleep(5 * 2**attempt)
    raise AssertionError("unreachable")


def reply_text(data: dict[str, Any]) -> str:
    if data.get("stop_reason") != "end_turn":
        raise SystemExit(f"translation stopped with stop_reason={data.get('stop_reason')!r}")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


# Additional trusted correction instructions, separate from the Markdown source bound.
MAX_FEEDBACK_BYTES = 4096


def request_system(lang: Language, *, initial_feedback: str = "") -> str:
    """Canonical SYSTEM bytes, including any bounded current-source correction feedback."""
    if len(initial_feedback) > MAX_FEEDBACK_BYTES or any(
        char != "\n" and not " " <= char <= "~" for char in initial_feedback
    ):
        raise ValueError("initial feedback must be printable ASCII or LF, at most 4096 bytes")
    system = SYSTEM.format(language=lang.english, style=lang.style)
    if initial_feedback:
        system += (
            "\n\nAdditional corrections for this source; preserve every rule above:\n"
            + initial_feedback
        )
    return system


def translate_text(
    text: str,
    lang: Language,
    client: Any,
    spend: Spend,
    *,
    source_context: context.SourceContext | None = None,
    initial_feedback: str = "",
) -> str:
    """Start with current input and optional SYSTEM feedback, without an old assistant turn."""
    system = request_system(lang, initial_feedback=initial_feedback)
    if initial_feedback and (source_context is None or not source_context.path):
        raise ValueError("initial feedback requires the current source context")
    if source_context and (
        not 0 <= source_context.start <= source_context.end <= len(source_context.english)
        or source_context.english[source_context.start : source_context.end] != text
    ):
        raise ValueError("translation request does not match its source context")
    if not text.strip():
        return text
    messages = [{"role": "user", "content": text}]
    for _ in range(2):
        spend.before_call()
        data = call(client, spend.model, system, messages)
        spend.add(data.get("usage", {}))
        reply = reply_text(data)
        problems = differences(text, reply, source_context=source_context)
        if not problems:
            return _same_edges(text, reply)
        messages += [
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": "Fix these differences and reply with the whole text again:\n"
                + "\n".join(problems),
            },
        ]
    raise TranslationDiffers(f"still differs after a retry: {problems}")


def _same_edges(source: str, text: str) -> str:
    """Keep the source's leading and trailing blank lines, so sections join as they cut."""
    lead = source[: len(source) - len(source.lstrip("\n"))]
    trail = source[len(source.rstrip("\n")) :]
    return lead + text.strip("\n") + trail


# ---- pages ----------------------------------------------------------------------------


def prepare(source: str, lang: Language) -> tuple[str, str]:
    """The English text to translate and the language line to put back afterwards ('' when
    the page has none)."""
    text = (ROOT / source).read_text()
    target = target_path(lang.code, source)
    match = LANGUAGE_LINE.search(text)
    line = language_line(target) if match else ""
    if match:
        text = text[: match.start()] + "<!-- languages -->" + text[match.end() :]
    return rewrite_links(text, source, lang.code), line


def render(source: str, lang: Language, body: str, line: str, model: str) -> str:
    if line:
        if "<!-- languages -->" not in body:
            raise SystemExit(f"{lang.code}: the translation of {source} dropped the language line")
        body = body.replace("<!-- languages -->", line, 1)
    return header(source, lang, model, blob(source)) + body


# Keep short pages together for consistent terms; larger requests also have a byte bound.
WHOLE_PAGE_CHARS = 8000


def english_requests(text: str) -> list[str]:
    """The bounded requests used by both translation and its cost/reservation planners."""
    whole = anchor_headings(text, Counter())
    try:
        groups = [whole] if len(whole) <= WHOLE_PAGE_CHARS else chunks.heading_sections(whole)
        return [piece for section in groups for piece in chunks.split(section)]
    except ValueError as error:
        raise TranslationDiffers(str(error)) from error


def translate_page(source: str, lang: Language, client: Any, spend: Spend) -> None:
    text, line = prepare(source, lang)
    requests = english_requests(text)
    english = "\n".join(requests)
    replies = []
    offset = 0
    for request in requests:
        scope = context.SourceContext(source, english, offset, offset + len(request))
        replies.append(translate_text(request, lang, client, spend, source_context=scope))
        offset += len(request) + 1
    body = "\n".join(replies)
    # A section can borrow an English label from another part of the page; `check` reads the
    # whole page, so the page is held to the same rule before it is written.
    problems = differences(english, body, source_path=source)
    if chunks.table_shape(english) != chunks.table_shape(body):
        problems.append("table row/cell layout differs after request assembly")
    if problems:
        raise TranslationDiffers(f"assembled page differs: {problems}")
    path = target_path(lang.code, source)
    lifecycle.stage(path, render(source, lang, body, line, spend.model))
    print(f"verified {path}  (${spend.usd:.2f} so far)", flush=True)


def write_language_lines(codes: list[str] | None = None) -> None:
    current = lifecycle.current_readme_outputs(codes)
    english_before = (ROOT / "README.md").read_text() if (ROOT / "README.md").is_file() else ""
    selected = None if codes is None else {target_path(code, "README.md") for code in codes}
    for path in language_lines_differ():
        if selected is not None and path != "README.md" and path not in selected:
            continue
        file = ROOT / path
        text = file.read_text()
        match = LANGUAGE_LINE.search(text)
        if not match:
            raise SystemExit(f"{path} has no <!-- languages --> markers")
        file.write_text(text[: match.start()] + language_line(path) + text[match.end() :])
    lifecycle.rebase_readme_outputs(current, english_before)


# ---- commands -------------------------------------------------------------------------


def check(skip_stale: bool = False) -> int:
    model = os.environ.get("LOOM_I18N_MODEL", DEFAULT_MODEL)
    pending = list(
        dict.fromkeys(stale(model=model) + refresh.work(refresh.plan(), include_blocked=True))
    )
    coverage = [f"{target_path(lang.code, source)} is missing or stale" for source, lang in pending]
    obsolete = orphans()
    coverage += [f"{path} is outside the current source inventory" for path in obsolete]
    problems = lifecycle.present_problems()
    if not skip_stale:
        problems += coverage
    invalid = len(problems) if skip_stale else len(problems) - len(coverage)
    print(f"pending={len(pending)} obsolete={len(obsolete)} invalid={invalid}")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def batch_model(cap: float) -> str:
    if not math.isfinite(cap) or cap < 0:
        raise SystemExit("LOOM_I18N_MAX_USD must be finite and nonnegative")
    model = os.environ.get("LOOM_I18N_MODEL", DEFAULT_MODEL)
    if model not in PRICES:
        raise SystemExit(
            f"LOOM_I18N_MODEL={model!r} has no price in PRICES; the cost cap needs one"
        )
    return model


def batch(
    every: bool, codes: list[str] | None, cap: float
) -> tuple[list[tuple[str, Language]], int]:
    """Select a stable whole-page batch; stale present pages precede missing pages."""
    model = batch_model(cap)
    languages = tuple(lg for lg in LANGUAGES if codes is None or lg.code in codes)
    job = refresh.plan(every, codes)
    forced = refresh.work(job, include_blocked=True)
    force_paths = {target_path(lg.code, source) for source, lg in forced}
    pairs = stale(languages, model) + [pair for pair in forced if pair[1] in languages]
    unique = list(dict.fromkeys(pairs))
    work = [pair for pair in unique if not refresh.blocked(target_path(pair[1].code, pair[0]), job)]
    work.sort(
        key=lambda pair: (
            not (ROOT / target_path(pair[1].code, pair[0])).is_file(),
            pair[0],
            pair[1].code,
        )
    )
    checkpoints = lifecycle.current_checkpoints()
    work = [
        (s, lg)
        for s, lg in work
        if (target_path(lg.code, s) not in checkpoints or target_path(lg.code, s) in force_paths)
        and not lifecycle.readme_publication_blocked(target_path(lg.code, s), codes, checkpoints)
    ]
    selected, cost = [], 0.0
    for source, lang in work:
        estimate = estimate_usd(model, request_chars(source, lang))
        if cost + estimate <= cap and cap > 0:
            selected.append((source, lang))
            cost += estimate
    return selected, len(unique) - len(selected)


def batch_reservation(every: bool = False, codes: list[str] | None = None, cap: float = 5) -> float:
    """Book all possible calls before a batch: two verifier attempts, four API attempts.

    Input bytes bound source tokens; retry input includes the generated reply and feedback.
    The allowance is deliberately conservative and is never refunded after an uncertain call.
    """
    model = os.environ.get("LOOM_I18N_MODEL", DEFAULT_MODEL)
    work, _ = batch(every, codes, cap)
    price_in, price_out = PRICES[model]
    total = 0.0
    for source, lang in work:
        system = SYSTEM.format(language=lang.english, style=lang.style)
        for request in english_requests(prepare(source, lang)[0]):
            size = len((system + request).encode()) + 4096
            total += 4 * ((3 * size + 2 * MAX_TOKENS) * price_in + 2 * MAX_TOKENS * price_out) / 1e6
    return math.ceil(total * 1e6) / 1e6


def import_pending(ref: str, codes: list[str] | None = None) -> int:
    return lifecycle.import_pending(ref, codes)


def update(every: bool = False, codes: list[str] | None = None) -> int:
    cap = float(os.environ.get("LOOM_I18N_MAX_USD", "5"))
    model = batch_model(cap)
    lifecycle.require_owned_outputs()
    refresh.clear_merged()
    if job := refresh.plan(every, codes):
        refresh.save(job)
    work, deferred = batch(every, codes, cap)
    for lang in LANGUAGES:
        path = target_path(lang.code, "README.md")
        if (codes is None or lang.code in codes) and lifecycle.readme_publication_blocked(
            path, codes, lifecycle.current_checkpoints()
        ):
            print(f"deferred {path}: README language links require other locales")
    spend = Spend(model, cap)
    written = 0
    failed: list[str] = []
    write_language_lines(codes)
    try:
        client = bedrock_client() if work else None
        for index, (source, lang) in enumerate(work):
            try:
                base = refresh.published_hash(target_path(lang.code, source))
                translate_page(source, lang, client, spend)
                refresh.record(source, lang, base)
                written += 1
            except TranslationDiffers as exc:
                failed.append(f"{target_path(lang.code, source)}: {exc}")
            except (Exception, SystemExit) as exc:  # Report progress before refusing.
                failed.append(f"{target_path(lang.code, source)}: {exc}")
                deferred += len(work) - index - 1
                raise
    finally:
        # Preserve verified progress even when a later page fails.
        lifecycle.activate_ready(codes)
        removed, blocked = lifecycle.retire_obsolete(codes)
        print(f"obsolete_removed={removed} obsolete_deferred={blocked}")
        print(
            f"translated={written} failed={len(failed)} deferred={deferred} "
            f"obsolete={len(orphans())}"
        )
        print(
            f"spent: {spend.input_tokens} input, {spend.output_tokens} output tokens, "
            f"${spend.usd:.2f}"
        )
        for failure in failed:
            print(f"not written: {failure}")
    if failed:
        raise SystemExit("pages not written:\n" + "\n".join(failed))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check", "update"))
    parser.add_argument("--all", action="store_true", help="translate every page, stale or not")
    parser.add_argument("--lang", action="append", choices=[lg.code for lg in LANGUAGES])
    parser.add_argument("--skip-stale", action="store_true", help="check: everything but freshness")
    args = parser.parse_args(argv)
    if args.command == "check":
        if args.all or args.lang:
            parser.error("--all and --lang go with update")
        return check(args.skip_stale)
    if args.skip_stale:
        parser.error("--skip-stale goes with check")
    codes = args.lang if args.lang is not None else list(ROLLOUT_LANGUAGES)
    if not set(codes) <= set(ROLLOUT_LANGUAGES):
        parser.error("--lang is outside the active translation rollout")
    return update(args.all, codes)


if __name__ == "__main__":
    raise SystemExit(main())
