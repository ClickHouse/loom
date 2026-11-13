"""Machine translations of a fixed set of English pages into i18n/<lang>/.

    python -m scripts.i18n.translate check   # exit 1: a translation is missing or stale
    python -m scripts.i18n.translate update  # translate those pages (needs ANTHROPIC_API_KEY)

Each page is sent to the Messages API one `## ` section at a time. A section's
translation is kept only when its fenced code blocks, inline code spans, link targets,
heading anchors and numbers match the English section; otherwise the call is retried once
with the differences named, then the run fails. Relative links are rewritten before
translation: a page of the set links to its translation, anything else to the English file
on GitHub. Every heading gets an explicit `<a id>` with its English anchor, so `#anchor`
links keep working on the translated page. The first line of each translation stamps the
git blob of the English page it was made from; `check` compares that stamp with the file.

The run estimates its cost before the first call and stops when the estimate, or the
spend reported by the API, passes LOOM_I18N_MAX_USD (default 5).
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
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


LANGUAGES = (
    Language(
        "zh-CN",
        "简体中文",
        "Simplified Chinese",
        "本页由机器从[英文原文]({url})翻译，内容以英文原文为准。",
    ),
    Language(
        "ja",
        "日本語",
        "Japanese",
        "このページは[英語の原文]({url})の機械翻訳です。内容は英語の原文が優先されます。",
    ),
    Language(
        "ko",
        "한국어",
        "Korean",
        "이 페이지는 [영어 원문]({url})을 기계 번역한 것이며, 영어 원문이 기준입니다.",
    ),
    Language(
        "pt-BR",
        "Português (Brasil)",
        "Brazilian Portuguese",
        "Esta página é uma tradução automática da [página em inglês]({url}), que prevalece.",
    ),
    Language(
        "es",
        "Español",
        "Spanish",
        "Esta página es una traducción automática de la [página en inglés]({url}), que prevalece.",
    ),
    Language(
        "ru",
        "Русский",
        "Russian",
        "Эта страница — машинный перевод [английской страницы]({url}); "
        "при расхождениях верна английская версия.",
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
LINK = re.compile(r"(\]\(|(?:href|src)=\")([^)\"\s]+)")
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
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to hyphens,
    a repeated anchor numbered from -1."""
    text = re.sub(r"`|\*\*|__|\[([^\]]*)\]\([^)]*\)", r"\1", heading).lower()
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


def rewrite_links(text: str, source: str, lang: str) -> str:
    """Point every relative link of `source` at its target as seen from the translation."""
    here = posixpath.dirname(target_path(lang, source))

    def fix(match: re.Match[str]) -> str:
        prefix, link = match.groups()
        if re.match(r"[a-z]+:|#|/", link):
            return match.group(0)
        path, _, anchor = link.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        suffix = f"#{anchor}" if anchor else ""
        if resolved in SOURCES:
            return prefix + posixpath.relpath(target_path(lang, resolved), here) + suffix
        kind = "raw" if prefix.startswith("src") or _is_image(text, match.start()) else "blob"
        return f"{prefix}{REPO_URL}/{kind}/main/{resolved}{suffix}"

    return LINK.sub(fix, text)


def _is_image(text: str, at: int) -> bool:
    """A Markdown link at `at` is an image when its `[` follows a `!`."""
    opening = text.rfind("[", 0, at)
    return opening > 0 and text[opening - 1] == "!"


def language_line(current: str, source: str) -> str:
    """The README's language links as seen from `current` (a path relative to the root)."""
    here = posixpath.dirname(current)
    items = [("README.md", "English")] + [
        (target_path(lg.code, source), lg.name) for lg in LANGUAGES
    ]
    links = [
        name
        if path == current
        else f'<a href="{posixpath.relpath(path, here) if here else "./" + path}">{name}</a>'
        for path, name in items
    ]
    return "<!-- languages -->\n  " + " · ".join(links) + "\n  <!-- /languages -->"


# ---- verification ---------------------------------------------------------------------


def invariants(text: str) -> dict[str, Counter[str]]:
    """What a translation must carry over unchanged."""
    code, prose = [], []
    for line, in_code in outside_fences(text):
        (code if in_code else prose).append(line)
    body = "\n".join(prose)
    without_spans = CODE_SPAN.sub(" ", body)
    without_links = LINK.sub(" ", without_spans)
    return {
        "code block lines": Counter(code),
        "inline code": Counter(" ".join(m.group(2).split()) for m in CODE_SPAN.finditer(body)),
        "link targets": Counter(m.group(2) for m in LINK.finditer(body)),
        "anchors": Counter(m.group(1) for m in map(ANCHOR.match, prose) if m),
        "headings": Counter(m.group(1) for m in map(HEADING.match, prose) if m),
        "numbers": Counter(NUMBER.findall(without_links)),
    }


def differences(source: str, translation: str) -> list[str]:
    """How `translation` breaks the invariants of `source`; empty when it keeps them.
    A translation may add a number (a spelled-out one written as digits), never drop one."""
    want, got = invariants(source), invariants(translation)
    found = []
    for name, expected in want.items():
        actual = got[name]
        missing = expected - actual
        extra = actual - expected
        if name == "numbers":
            extra = Counter()
        if missing or extra:
            found.append(f"{name}: missing {sorted(missing)[:8]}, unexpected {sorted(extra)[:8]}")
    return found


# ---- stamps ---------------------------------------------------------------------------


def blob(source: str) -> str:
    return subprocess.run(
        ["git", "hash-object", source], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def stale() -> list[tuple[str, Language]]:
    """Every (source, language) whose translation is missing or stamped with another blob."""
    out = []
    for source in SOURCES:
        current = blob(source)
        for lang in LANGUAGES:
            path = ROOT / target_path(lang.code, source)
            stamp = STAMP.match(path.read_text()) if path.is_file() else None
            if not stamp or stamp.group(1) != source or stamp.group(2) != current:
                out.append((source, lang))
    return out


def readme_language_line_differs() -> bool:
    text = (ROOT / "README.md").read_text()
    match = LANGUAGE_LINE.search(text)
    return not match or match.group(0) != language_line("README.md", "README.md")


# ---- the API --------------------------------------------------------------------------


@dataclass
class Spend:
    model: str
    cap_usd: float
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def usd(self) -> float:
        price_in, price_out = PRICES[self.model]
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1e6

    def add(self, usage: dict[str, int]) -> None:
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        if self.usd > self.cap_usd:
            raise SystemExit(
                f"spent ${self.usd:.2f}, over LOOM_I18N_MAX_USD=${self.cap_usd:.2f}; stopping"
            )


def estimate_usd(model: str, chars: int) -> float:
    """An upper estimate: about three characters per input token, and a reply up to twice
    the input's tokens (thinking and scripts that need more tokens per character)."""
    price_in, price_out = PRICES[model]
    tokens = chars / 3
    return (tokens * price_in + 2 * tokens * price_out) / 1e6


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
        except (urllib.error.URLError, TimeoutError) as e:
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
    system = SYSTEM.format(language=lang.english)
    messages = [{"role": "user", "content": section}]
    for _ in range(2):
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
    line = language_line(target, source) if match else ""
    if match:
        text = text[: match.start()] + "<!-- languages -->" + text[match.end() :]
    return rewrite_links(text, source, lang.code), line


def render(source: str, lang: Language, body: str, line: str, model: str) -> str:
    url = f"{REPO_URL}/blob/main/{source}"
    if line:
        if "<!-- languages -->" not in body:
            raise SystemExit(f"{lang.code}: the translation of {source} dropped the language line")
        body = body.replace("<!-- languages -->", line, 1)
    stamp = f"<!-- i18n: source={source} blob={blob(source)} model={model} -->\n"
    english = f"Machine-translated from the [English page]({url}), which is authoritative."
    banner = f"> {lang.banner.format(url=url)}\n> {english}\n\n"
    return stamp + banner + body


def translate_page(source: str, lang: Language, key: str, spend: Spend) -> None:
    text, line = prepare(source, lang)
    seen: Counter[str] = Counter()
    english = [anchor_headings(s, seen) for s in sections(text)]
    body = "\n".join(translate_section(s, lang, key, spend) for s in english)
    path = ROOT / target_path(lang.code, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(source, lang, body, line, spend.model))
    print(f"{path.relative_to(ROOT)}  (${spend.usd:.2f} so far)", flush=True)


def write_readme_language_line() -> None:
    path = ROOT / "README.md"
    text = path.read_text()
    path.write_text(
        LANGUAGE_LINE.sub(lambda _: language_line("README.md", "README.md"), text, count=1)
    )


# ---- commands -------------------------------------------------------------------------


def check() -> int:
    problems = [
        f"{target_path(lang.code, source)} is missing or older than {source}"
        for source, lang in stale()
    ]
    if readme_language_line_differs():
        problems.append("README.md's language line differs from the generated one")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def update() -> int:
    model = os.environ.get("LOOM_I18N_MODEL", DEFAULT_MODEL)
    if model not in PRICES:
        raise SystemExit(
            f"LOOM_I18N_MODEL={model!r} has no price in PRICES; the cost cap needs one"
        )
    spend = Spend(model, float(os.environ.get("LOOM_I18N_MAX_USD", "5")))
    if readme_language_line_differs():
        write_readme_language_line()
    work = stale()
    estimate = estimate_usd(model, sum(len((ROOT / s).read_text()) for s, _ in work))
    print(f"{len(work)} page(s) to translate with {model}, estimated at most ${estimate:.2f}")
    if estimate > spend.cap_usd:
        raise SystemExit(
            f"estimate over LOOM_I18N_MAX_USD=${spend.cap_usd:.2f}; nothing translated"
        )
    if work:
        key = os.environ.get("ANTHROPIC_API_KEY") or sys.exit("ANTHROPIC_API_KEY is not set")
        for source, lang in work:
            translate_page(source, lang, key, spend)
    print(
        f"done: {spend.input_tokens} input, {spend.output_tokens} output tokens, ${spend.usd:.2f}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check", "update"))
    args = parser.parse_args(argv)
    return check() if args.command == "check" else update()


if __name__ == "__main__":
    raise SystemExit(main())
