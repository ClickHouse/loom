"""Machine translations of a fixed set of English pages into i18n/<lang>/.

    python -m scripts.i18n.translate check    # exit 1: a translation is missing, stale or orphaned
    python -m scripts.i18n.translate update   # translate the stale pages (needs ANTHROPIC_API_KEY)
    python -m scripts.i18n.translate update --all [--lang ja ...]   # after a prompt change

Each page is sent to the Messages API one `## ` section at a time. A section's
translation is kept only when its fenced code blocks (in order), inline code spans, link
targets, URLs, heading anchors and numbers match the English section; otherwise the call is
retried once with the differences named, then the run fails. Relative links in the prose
are rewritten before translation: a page of the set links to its translation, anything
else to the English file on GitHub. Every heading gets an explicit `<a id>` with its
English anchor, so `#anchor` links keep working on the translated page. The first line of
each translation stamps the git blob of the English page it was made from; `check`
compares that stamp with the file.
A change to the model or a banner makes every page stale; a change to the prompt does not:
regenerate with `--all`.

The run estimates its cost before the first call and stops when the estimate, or the
spend reported by the API, passes LOOM_I18N_MAX_USD (default 5). Every call checks the cap
first, so the spend passes it by at most the calls already in flight, one per worker. The
first page that fails stops the others before their next call; the pages finished by then
are written.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = "i18n"
REPO_URL = "https://github.com/ClickHouse/loom"

SOURCES = (
    "README.md",
    "docs/getting-started/quickstart.md",
    "docs/use-cases/index.md",
    "docs/use-cases/agent-memory.md",
    "docs/use-cases/autonomous-agents.md",
    "docs/use-cases/code-intelligence.md",
    "docs/use-cases/coding-agent-cost.md",
    "docs/use-cases/document-search.md",
    "docs/use-cases/multi-agent.md",
    "docs/use-cases/runs-and-pr-history.md",
    "docs/use-cases/self-hosted-clickhouse.md",
)


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
        "paragraph into one line. Fixed terms: dashboard 控制台, an access token 令牌 (model "
        "tokens stay token), namespace 命名空间, memory 记忆; keep agent as agent.",
    ),
    Language(
        "ja",
        "日本語",
        "Japanese",
        "このページは[英語の原文]({url})の機械翻訳です。内容は英語の原文が優先されます。",
        "Use Japanese punctuation (、。：（）) in prose, and join the lines of a paragraph "
        "into one line. Fixed terms: dashboard ダッシュボード, token トークン, namespace "
        "名前空間, memory メモリ, agent エージェント.",
    ),
    Language(
        "ko",
        "한국어",
        "Korean",
        "이 페이지는 [영어 원문]({url})을 기계 번역한 것이며, 영어 원문이 기준입니다.",
        "Fixed terms: dashboard 대시보드, token 토큰, namespace 네임스페이스, memory 메모리, "
        "agent 에이전트.",
    ),
    Language(
        "pt-BR",
        "Português (Brasil)",
        "Brazilian Portuguese",
        "Esta página é uma tradução automática da [página em inglês]({url}), que prevalece.",
        "Fixed terms: dashboard painel, token token, namespace namespace, memory memória, "
        "agent agente.",
    ),
    Language(
        "es",
        "Español",
        "Spanish",
        "Esta página es una traducción automática de la [página en inglés]({url}), que prevalece.",
        "Fixed terms: dashboard panel, token token, namespace espacio de nombres, "
        "memory memoria, agent agente.",
    ),
    Language(
        "ru",
        "Русский",
        "Russian",
        "Эта страница — машинный перевод [английской страницы]({url}); "
        "при расхождениях верна английская версия.",
        "Fixed terms: dashboard панель управления, token токен, namespace пространство имён, "
        "memory память, agent агент.",
    ),
)

# USD per million tokens (input, output). A model missing here is refused: the cost cap
# needs its price.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
DEFAULT_MODEL = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 32000

LANGUAGE_LINE = re.compile(r"<!-- languages -->.*?<!-- /languages -->", re.DOTALL)
STAMP = re.compile(r"\A<!-- i18n: source=(\S+) blob=([0-9a-f]{40}) model=(\S+) -->\n")
FENCE = re.compile(r"^(`{3,}|~{3,})")
# A Markdown link or image target, an HTML href or src, or a reference definition.
LINK = re.compile(r"(\]\(|(?:href|src)=\"|^\[(?!\^)[^\]\n]+\]:[ \t]+)([^)\"\s]+)", re.MULTILINE)
# A link's text and target; a link to an English-only page may keep that page's title.
LINK_TEXT = re.compile(r"(?<!!)\[([^\[\]\n]+)\]\(([^)\s]+)")
# Words a heading or link text may keep in every language: names, and the two terms
# some glossaries keep as they are.
KEPT_WORDS = frozenset(
    """loom clickhouse postgres claude code codex cursor docker compose mcp sdk sdks python
    typescript node langgraph strands kubernetes helm github codespaces http https api cli
    okf rag llm json yaml sql oidc scim tls otel clickstack langfuse gemini vs windsurf zed
    cline pypi npm apache ci url id token namespace""".split()
)
URL = re.compile(r"https?://[^\s<>()\"'`*\]]+")
# A code span may wrap across lines; Markdown renders the break as a space.
CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
HEADING = re.compile(r"^(#{1,6}) +(.+?) *$")
ANCHOR = re.compile(r'^<a id="([^"]+)"></a>$')
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

SYSTEM = """You translate one section of a Markdown page of Loom's documentation into {language}.
Loom is an open-source memory, document search and code index server for AI agents.
Rules:
- Translate the prose, headings, table cells, list items, alt text and link text.
- Copy byte for byte: fenced code blocks, inline code spans, URLs and link targets, HTML
  tags and attributes, lines of the form <a id="..."></a>, and every number.
- Keep product, company and project names, CLI commands, flags, file names and op names
  in English (Loom, ClickHouse, Postgres, Claude Code, Codex, Docker, MCP, SDK).
- Keep the Markdown structure: the same headings in the same order, the same list and
  table layout, the same blank lines between blocks.
- Write the plain, technical register of developer documentation in {language}.
- {style}
Reply with the translated section only, with no preamble and no code fence around it."""


# ---- page structure -------------------------------------------------------------------


def outside_fences(text: str) -> list[tuple[str, bool]]:
    """The page's lines, each with whether it is inside a fenced code block."""
    lines, fence = [], None
    for line in text.split("\n"):
        mark = FENCE.match(line.lstrip())
        if fence is None and mark:
            fence = mark.group(1)
            lines.append((line, True))
        elif fence is not None:
            lines.append((line, True))
            if line.strip().startswith(fence) and line.strip().strip(fence[0]) == "":
                fence = None
        else:
            lines.append((line, False))
    return lines


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


def sections(text: str) -> list[str]:
    """The page cut before every `## ` heading outside code; the pieces join back to it
    with newlines."""
    pieces: list[list[str]] = [[]]
    for line, in_code in outside_fences(text):
        if not in_code and line.startswith("## ") and pieces[-1]:
            pieces.append([])
        pieces[-1].append(line)
    return ["\n".join(p) for p in pieces]


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

    def fix(prose: str, match: re.Match[str]) -> str:
        prefix, link = match.groups()
        if re.match(r"[a-z]+:|#|/", link):
            return match.group(0)
        path, _, anchor = link.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        suffix = f"#{anchor}" if anchor else ""
        if resolved in SOURCES:
            return prefix + posixpath.relpath(target_path(lang, resolved), here) + suffix
        image = prefix == 'src="' or (prefix == "](" and _is_image(prose, match.start()))
        return f"{prefix}{REPO_URL}/{'raw' if image else 'blob'}/main/{resolved}{suffix}"

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


def language_line(current: str) -> str:
    """The README's language links as seen from `current` (a path relative to the root)."""
    here = posixpath.dirname(current)
    items = [("README.md", "English")] + [
        (target_path(lg.code, "README.md"), lg.name) for lg in LANGUAGES
    ]
    links = [
        name
        if path == current
        else f'<a href="{posixpath.relpath(path, here) if here else "./" + path}">{name}</a>'
        for path, name in items
    ]
    return "<!-- languages -->\n  " + " · ".join(links) + "\n  <!-- /languages -->"


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
    }


def labels(text: str) -> Counter[str]:
    """The headings, and the texts of Markdown links within the translated pages, that hold
    a word to translate. HTML link texts (the README's navigation row) are not checked."""
    prose = "\n".join(line for line, in_code in outside_fences(text) if not in_code)
    found = [m.group(2) for m in map(HEADING.match, prose.split("\n")) if m]
    found += [label for label, target in LINK_TEXT.findall(prose) if not URL.match(target)]
    out: Counter[str] = Counter()
    for label in found:
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", CODE_SPAN.sub(" ", label))
        if any(w.lower() not in KEPT_WORDS for w in words):
            out[label] += 1
    return out


def differences(source: str, translation: str) -> list[str]:
    """How `translation` breaks the invariants of `source`; empty when it keeps them.
    A translation may add a number (a spelled-out one written as digits), never drop one,
    and may not hand back a heading or link text in English."""
    found = []
    untranslated = labels(source) & labels(translation)
    if untranslated:
        found.append(f"untranslated headings or link texts: {sorted(untranslated)[:8]}")
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


def require_sources() -> None:
    missing = [s for s in SOURCES if not (ROOT / s).is_file()]
    if missing:
        raise SystemExit(f"in SOURCES but missing: {', '.join(missing)}; update SOURCES")


def blob(source: str) -> str:
    return subprocess.run(
        ["git", "hash-object", source], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def header(source: str, lang: Language, model: str, blob_sha: str) -> str:
    """A translation's first lines: the stamp and the two banner lines."""
    url = f"{REPO_URL}/blob/main/{source}"
    english = f"Machine-translated from the [English page]({url}), which is authoritative."
    stamp = f"<!-- i18n: source={source} blob={blob_sha} model={model} -->\n"
    return f"{stamp}> {lang.banner.format(url=url)}\n> {english}\n\n"


def stale(
    languages: tuple[Language, ...] = LANGUAGES, model: str = DEFAULT_MODEL
) -> list[tuple[str, Language]]:
    """Every (source, language) whose translation is missing, or whose stamp or banner
    differs: another English blob, another model, another banner."""
    out = []
    for source in SOURCES:
        current = blob(source)
        for lang in languages:
            path = ROOT / target_path(lang.code, source)
            text = path.read_text() if path.is_file() else ""
            if not text.startswith(header(source, lang, model, current)):
                out.append((source, lang))
    return out


def orphans() -> list[str]:
    """Files under i18n/ that no (source, language) pair writes."""
    expected = {target_path(lang.code, s) for s in SOURCES for lang in LANGUAGES}
    found = {p.relative_to(ROOT).as_posix() for p in (ROOT / OUT_DIR).rglob("*") if p.is_file()}
    return sorted(found - expected)


def prose_links(text: str) -> list[re.Match[str]]:
    """The link matches of `text` outside fenced code and code spans."""
    found: list[re.Match[str]] = []

    def collect(prose: str) -> str:
        found.extend(LINK.finditer(prose))
        return prose

    in_prose(text, collect)
    return found


def link_problems(stale_pages: frozenset[str] = frozenset()) -> list[str]:
    """Relative links and `#anchors` in i18n/ that point at nothing. An anchor into or out of
    one of `stale_pages` is not checked: that page still carries its old English's anchors
    and links."""
    problems = []
    for file in sorted((ROOT / OUT_DIR).rglob("*.md")):
        path = file.relative_to(ROOT).as_posix()
        for match in prose_links(file.read_text()):
            link, _, anchor = match.group(2).partition("#")
            if re.match(r"[a-z]+:", link):
                continue
            target = (
                posixpath.normpath(posixpath.join(posixpath.dirname(path), link)) if link else path
            )
            if not (ROOT / target).is_file():
                problems.append(f"{path} links to {match.group(2)}, which does not exist")
            elif target in stale_pages or path in stale_pages:
                continue
            elif anchor and f'<a id="{anchor}"></a>' not in (ROOT / target).read_text():
                problems.append(f"{path} links to {match.group(2)}, which has no such anchor")
    return problems


def untranslated_pages() -> list[str]:
    """Translations that kept a heading, or a link text within the set, in English."""
    problems = []
    for source in SOURCES:
        for lang in LANGUAGES:
            path = ROOT / target_path(lang.code, source)
            if not path.is_file():
                continue
            english = "\n".join(english_sections(prepare(source, lang)[0]))
            kept = labels(english) & labels(path.read_text())
            if kept:
                rel = path.relative_to(ROOT).as_posix()
                problems.append(f"{rel} keeps in English: {sorted(kept)[:4]}")
    return problems


def readme_language_line_differs() -> bool:
    text = (ROOT / "README.md").read_text()
    match = LANGUAGE_LINE.search(text)
    return not match or match.group(0) != language_line("README.md")


# ---- the API --------------------------------------------------------------------------


@dataclass
class Spend:
    """What a run has spent, shared by its workers; `stopped` tells them to make no more calls."""

    model: str
    cap_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    stopped: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def usd(self) -> float:
        price_in, price_out = PRICES[self.model]
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1e6

    def before_call(self) -> None:
        if self.stopped.is_set():
            raise SystemExit("stopped: another page failed or the cap was reached")
        with self.lock:
            spent = self.usd
        if spent > self.cap_usd:
            self.stopped.set()
            raise SystemExit(f"spent ${spent:.2f}, over LOOM_I18N_MAX_USD=${self.cap_usd:.2f}")

    def add(self, usage: dict[str, int]) -> None:
        with self.lock:
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)


def request_chars(source: str, lang: Language) -> int:
    """The characters one translation of `source` sends: each section with the system prompt."""
    text, _ = prepare(source, lang)
    system = len(SYSTEM.format(language=lang.english, style=lang.style))
    return sum(len(s) + system for s in english_sections(text))


def estimate_usd(model: str, chars: int) -> float:
    """About three characters per input token, and a reply of 1.2 times the input's tokens.
    Measured runs cost from about 20% under the estimate to 6% over it (retries add input
    the estimate does not see). `Spend` is the hard cap; this only refuses a run that cannot fit."""
    price_in, price_out = PRICES[model]
    tokens = chars / 3
    return (tokens * price_in + 1.2 * tokens * price_out) / 1e6


def call(key: str, model: str, system: str, messages: list[dict[str, str]]) -> dict:
    body = json.dumps(
        {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
            "output_config": {"effort": "low"},
        }
    ).encode()
    headers = {
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    for attempt in range(4):
        request = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 529) or attempt == 3:
                raise SystemExit(
                    f"Messages API {e.code}: {e.read().decode(errors='replace')[:500]}"
                ) from e
        except TimeoutError as e:
            # The request was sent and may be billed; a retry would spend unseen tokens.
            raise SystemExit(f"Messages API timed out: {e}") from e
        except urllib.error.URLError as e:
            if attempt == 3:
                raise SystemExit(f"Messages API unreachable: {e}") from e
        time.sleep(5 * 2**attempt)
    raise AssertionError("unreachable")


def reply_text(data: dict) -> str:
    if data.get("stop_reason") != "end_turn":
        raise SystemExit(f"translation stopped with stop_reason={data.get('stop_reason')!r}")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def translate_section(section: str, lang: Language, key: str, spend: Spend) -> str:
    if not section.strip():
        return section
    system = SYSTEM.format(language=lang.english, style=lang.style)
    messages = [{"role": "user", "content": section}]
    for _ in range(2):
        spend.before_call()
        data = call(key, spend.model, system, messages)
        spend.add(data.get("usage", {}))
        text = reply_text(data)
        problems = differences(section, text)
        if not problems:
            return _same_edges(section, text)
        messages += [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": "Fix these differences and reply with the whole section:\n"
                + "\n".join(problems),
            },
        ]
    raise SystemExit(f"{lang.code}: a section still differs after a retry: {problems}")


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


def english_sections(text: str) -> list[str]:
    """The page's sections as sent, each heading with its page-wide anchor."""
    seen: Counter[str] = Counter()
    return [anchor_headings(s, seen) for s in sections(text)]


def translate_page(source: str, lang: Language, key: str, spend: Spend) -> None:
    text, line = prepare(source, lang)
    body = "\n".join(translate_section(s, lang, key, spend) for s in english_sections(text))
    path = ROOT / target_path(lang.code, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(source, lang, body, line, spend.model))
    print(f"{path.relative_to(ROOT)}  (${spend.usd:.2f} so far)", flush=True)


def write_readme_language_line() -> None:
    path = ROOT / "README.md"
    text = path.read_text()
    path.write_text(LANGUAGE_LINE.sub(lambda _: language_line("README.md"), text, count=1))


# ---- commands -------------------------------------------------------------------------


def check(skip_stale: bool = False) -> int:
    require_sources()
    model = os.environ.get("LOOM_I18N_MODEL", DEFAULT_MODEL)
    pending = stale(model=model)
    problems = [
        f"{target_path(lang.code, source)} is missing, or older than {source}, the model or "
        "the banner"
        for source, lang in pending
        if not skip_stale
    ]
    problems += [f"{path} is translated from no page in SOURCES" for path in orphans()]
    # After a partial run, a page that failed keeps its old anchors until it is translated.
    waiting = (
        frozenset(target_path(lang.code, s) for s, lang in pending) if skip_stale else frozenset()
    )
    problems += link_problems(waiting)
    problems += untranslated_pages()
    if readme_language_line_differs():
        problems.append("README.md's language line differs from the generated one")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


# Pages translated at once; each page's sections still go one after another.
WORKERS = 6


def update(every: bool = False, codes: list[str] | None = None) -> int:
    require_sources()
    model = os.environ.get("LOOM_I18N_MODEL", DEFAULT_MODEL)
    if model not in PRICES:
        raise SystemExit(
            f"LOOM_I18N_MODEL={model!r} has no price in PRICES; the cost cap needs one"
        )
    spend = Spend(model, float(os.environ.get("LOOM_I18N_MAX_USD", "5")))
    languages = tuple(lg for lg in LANGUAGES if not codes or lg.code in codes)
    if readme_language_line_differs():
        write_readme_language_line()
    work = [(s, lg) for s in SOURCES for lg in languages] if every else stale(languages, model)
    estimate = estimate_usd(model, sum(request_chars(s, lg) for s, lg in work))
    print(f"{len(work)} page(s) to translate with {model}, estimated at about ${estimate:.2f}")
    if estimate > spend.cap_usd:
        raise SystemExit(
            f"estimate over LOOM_I18N_MAX_USD=${spend.cap_usd:.2f}; nothing translated"
        )
    if work:
        key = os.environ.get("ANTHROPIC_API_KEY") or sys.exit("ANTHROPIC_API_KEY is not set")
        try:
            with ThreadPoolExecutor(WORKERS) as pool:
                futures = [pool.submit(translate_page, s, lg, key, spend) for s, lg in work]
                done, _ = wait(futures, return_when=FIRST_EXCEPTION)
                failed = [f for f in done if f.exception()]
                if failed:
                    spend.stopped.set()
                    pool.shutdown(cancel_futures=True)
                    raise SystemExit(f"{failed[0].exception()} (pages finished so far are written)")
        finally:
            # Printed on failure too, so a stopped run's spend is known.
            tokens = f"{spend.input_tokens} input, {spend.output_tokens} output tokens"
            print(f"spent: {tokens}, ${spend.usd:.2f}")
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
    return update(args.all, args.lang)


if __name__ == "__main__":
    raise SystemExit(main())
