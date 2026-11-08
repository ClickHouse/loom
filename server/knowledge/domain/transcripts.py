"""Markdown from a meeting recording's transcript. Pure: no I/O.

A transcript is a list of timed entries (who spoke, from when to when, what they
said). It becomes one `# mm:ss–mm:ss: <first words>` section per stretch of about
five minutes, so the chunker cuts at the minute and a citation of a chunk names
when it was said. Inside a section, one paragraph per speaker turn:
`<speaker>: <text>`. Speaker names are text of the transcript like any other.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime

# Seconds of a meeting one section covers before the next one starts.
SECTION_SECONDS = 300  # not a setting: a property of the conversion, the same for every source
# Words of a section's first line its heading repeats.
HEADING_WORDS = 8  # not a setting: a property of the conversion

# A cue's timing line: `[hh:]mm:ss.ttt --> [hh:]mm:ss.ttt`, then cue settings.
_TIME = r"(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_CUE_TIME = re.compile(rf"^({_TIME})\s+-->\s+({_TIME})")
# A cue line that names its speaker: `Name: text`.
_SPEAKER = re.compile(r"^([^:<>]{1,80}):\s+(.*)$")
# A WebVTT voice span names its speaker: `<v Name>text`. A class holds no blank,
# so a run of tabs is not split between the class and the blanks after it.
_VOICE = re.compile(r"^<v(?:\.[^\s>]*)?\s+([^>]{1,80})>(.*)$")
_TAG = re.compile(r"<[^>]*>")
# A line that would read as Markdown structure (a heading, a quote, a table, a fence,
# a Setext underline, an HTML block) and cut a section where the speaker did not.
_STRUCTURE = re.compile(r"^\s*[#>|=~`<-]")


@dataclass(frozen=True)
class Entry:
    """One stretch of speech: seconds from the start of the recording."""

    start: float
    end: float
    speaker: str
    text: str


def _seconds(stamp: str) -> float:
    """`[hh:]mm:ss.ttt` as seconds."""
    parts = stamp.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def _opens_cue(lines: list[str], i: int) -> bool:
    """Whether line `i` starts the next cue: its timing line, or a numeric cue id
    right above one. A cue whose text runs into the next cue ends there."""
    if _CUE_TIME.match(lines[i].strip()):
        return True
    following = i + 1 < len(lines) and _CUE_TIME.match(lines[i + 1].strip())
    return bool(following) and lines[i].strip().isdigit()


def parse_vtt(text: str) -> list[Entry]:
    """The cues of a WebVTT file, in order. A cue's speaker is its `<v Name>` span,
    or a leading `Name: ` when most of the file's cues start that way (so a line
    like `Note: ...` in a file without speakers stays text). A cue whose timing
    cannot be read is skipped."""
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[tuple[float, float, str, str]] = []
    i = 0
    while i < len(lines):
        timing = _CUE_TIME.match(lines[i].strip())
        i += 1
        if timing is None:
            continue
        payload: list[str] = []
        while i < len(lines) and lines[i].strip() and not _opens_cue(lines, i):
            payload.append(lines[i].strip())
            i += 1
        speaker, body = "", " ".join(payload)
        voice = _VOICE.match(body)
        if voice is not None:
            speaker, body = " ".join(voice.group(1).split()), voice.group(2)
        # no tag closes after the last `>`: reading only up to it keeps a run of
        # unclosed `<` from being rescanned from each one
        cut = body.rfind(">") + 1
        body = " ".join(html.unescape(_TAG.sub("", body[:cut]) + body[cut:]).split())
        if body:
            cues.append((_seconds(timing.group(1)), _seconds(timing.group(2)), speaker, body))
    unnamed = [c for c in cues if not c[2]]
    prefixed = sum(1 for c in unnamed if _SPEAKER.match(c[3]))
    use_prefix = prefixed * 2 > len(unnamed)
    entries: list[Entry] = []
    for start, end, speaker, body in cues:
        named = _SPEAKER.match(body) if use_prefix and not speaker else None
        if named is not None:
            speaker, body = named.group(1).strip(), named.group(2)
        entries.append(Entry(start, end, speaker, body))
    return entries


def parse_time(stamp: str) -> datetime | None:
    """An RFC 3339 time as the stores report it, or None."""
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def clock(seconds: float) -> str:
    """`mm:ss` from the start; the minutes run past 59 in a long meeting."""
    whole = max(0, int(seconds))
    return f"{whole // 60:02d}:{whole % 60:02d}"


def _line(text: str) -> str:
    """`text` as a paragraph that never reads as Markdown structure."""
    return "\\" + text if _STRUCTURE.match(text) else text


def _section(entries: list[Entry]) -> str:
    first = " ".join(entries[0].text.split()[:HEADING_WORDS])
    heading = f"# {clock(entries[0].start)}–{clock(max(e.end for e in entries))}: {first}"
    turns: list[tuple[str, list[str]]] = []
    for entry in entries:
        if turns and turns[-1][0] == entry.speaker:
            turns[-1][1].append(entry.text)
        else:
            turns.append((entry.speaker, [entry.text]))
    body = [
        _line(f"{speaker}: {' '.join(said)}" if speaker else " ".join(said))
        for speaker, said in turns
    ]
    return "\n\n".join([heading, *body])


def transcript_markdown(entries: list[Entry]) -> str:
    """The transcript's sections, in time order; '' when it has no entry."""
    ordered = sorted((e for e in entries if e.text.strip()), key=lambda e: e.start)
    sections: list[list[Entry]] = []
    for entry in ordered:
        if sections and entry.start - sections[-1][0].start < SECTION_SECONDS:
            sections[-1].append(entry)
        else:
            sections.append([entry])
    return "\n\n".join(_section(s) for s in sections)


__all__ = [
    "Entry",
    "HEADING_WORDS",
    "SECTION_SECONDS",
    "clock",
    "parse_time",
    "parse_vtt",
    "transcript_markdown",
]
