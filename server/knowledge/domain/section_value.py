"""Whether a document section is design content or noise: meeting notes and status reports,
their bookkeeping included, that carry nothing an engineer changing the code needs. Pure
and deterministic; no model call.

The label is on content, not on document type: a section of meeting notes that records a
decision ("agreed: drop the cache, its memory cost is too high") is content, and the
attendee list of a meeting note is noise. The rules leave a section as content when
unsure: a noise section missed only ranks higher than it should, while a design section
labelled noise leaves the context an agent is handed.

A section with a line that records a decision is content. Otherwise it is noise when it
lists questions with no answer under an open-questions heading (or is a deck's closing
"Questions?"), or when the document is meeting notes or a status report: there a section
that records no decision is discussion or bookkeeping (attendees, agenda, action items,
...), not design.

A document is notes by its title, in one of three shapes:
- a meeting or progress word, optionally followed by notes, minutes, update or report,
  then a date (2026-03-04, 2026/03/04, 3/4/26, March 4, 4 March) or a week: "Planner
  sync 2026-09-24", "Parser team status - week 31";
- a date first and the meeting word last: "2026-03-04 Meeting notes", "2026-03-04
  Storage sync";
- a meeting word in the title and a date as the top heading of each meeting (a running
  notes page).
Without a date the same words name a topic as often ("Standup reminder bot", "Order
sync 24/7"), and a title holding a design-document, release or incident word
("Calendar sync design 2026-01-15", "Release notes 2026-03-04") is never notes, unless
it is a recording's transcript (below). A document with no title is named by its first
heading. Lines inside a fenced code block are code, never questions.

Notes a meeting tool writes from a recording carry the meeting's date and clock time and
a transcript or notes-by mark in their title ("Planner sync - 2026/03/04 10:00 CET -
Notes by ..."). A notes-by mark names a recording only beside a meeting word and none of
the design-document words above; otherwise it signs a person's notes ("Outage 2026-03-04
14:00 UTC - notes by Ana"), which keep the rule above. A recording's summary and topic
sections are the tool's account of the meeting and stay content; only the transcript
(its clock-time segments) and the template around it (the invite header, next steps,
action items, attendees, the tool's footer) are noise, each unless it records a
decision.
"""

from __future__ import annotations

import re

from server.knowledge.domain.chunking import closes_fence, fence_marker

# A choice a team states for itself: "we'll go with", "we're switching to", "we picked",
# "let's use". A bare "we keep" is left out: "suggested we keep X" proposes, not decides.
_CHOICE_VERB = (
    r"(?:(?:going to |be )?(?:go(?:ing)?(?: ahead)? with|proceed(?:ing)? with|use|using|"
    r"keep|keeping|drop|dropping|"
    r"switch(?:ing)? to|move|moving|migrate|migrating|adopt|adopting|support|supporting|"
    r"ship|shipping|pick|choose|stick with|stay with|replace|replacing))"
)
# A plan to change something: "we'll enable", "I'm going to remove", "we're deprecating".
# The plain verb only after a future ("we are lower than the target" states a status).
_PLAN_VERB = r"(?:enable|disable|lower|remove|deprecate|disallow|roll out)\b"
_PLAN_ING = r"(?:enabling|disabling|lowering|removing|deprecating|disallowing|rolling out)\b"
_CHOSE = (
    r"(?:picked|chose|selected|switched to|moved to|migrated to|adopted|went with|dropped|"
    r"replaced|rejected|accepted)"
)
# A recorded decision: content wherever it is written. A phrase saying none was reached
# (`_UNDECIDED`) is not one, in meeting notes and in a heading of any document.
_DECISION = re.compile(
    r"\b(?:decided|decision|agreed|agreement|consensus|settled on|approved|signed off|"
    r"going with|going ahead with|proceeding with|went with|chosen|ruled out|resolved:|"
    r"conclusion:|outcome:|opted (?:for|to)|concluded|not supporting|"
    r"defaults? to|"
    rf"we(?:'re| are|'ll| will| won't| will not)(?: not)? {_CHOICE_VERB}|"
    # For "I", only a plan ("I'll remove the old flag", not "I'll move on").
    rf"(?:we|i)(?:'ll| will| won't| will not|'m going to| am going to|'re going to|"
    rf" are going to)(?: not)? {_PLAN_VERB}|"
    rf"let's {_PLAN_VERB}|we(?:'re| are) {_PLAN_ING}|"
    r"the plan is to|"
    rf"(?:we|the team)(?:'ve| have| had)? {_CHOSE}|"
    r"(?:was|were|is|are) (?:picked|selected)|"
    r"(?:option|proposal|approach)\b[^.;]{0,80}\b(?:accepted|rejected)\b|"
    rf"let's {_CHOICE_VERB})",
    re.IGNORECASE,
)
# Wording that says a discussion reached no decision.
_NO_DECISION = (
    r"(?:no (?:conclusion|decision|consensus|agreement)|undecided|"
    r"not (?:yet )?(?:decided|concluded|agreed)|"
    r"to be decided|(?:haven't|hasn't|have not|has not)(?: yet)?(?: been)? decided|"
    r"decision (?:pending|needed|deferred)|needs? a decision)"
)
# Where a label's value ends: the line, a stop, or a free-standing dash ("open -- later").
_LABEL_END = r"(?: yet)?(?=\W*$|\s*[.;,(\u2013\u2014]|\s+-+(?:\s|$))"
_UNDECIDED = re.compile(
    # A label whose whole value says none was reached: "Outcome: no decision yet",
    # "Decision: open", "Decision: deferred to next sync". "Decision: open the replica to
    # reads", "Decision: deferred the rewrite", "Decision: open-source the parser" and
    # "Decision: no consensus on Kafka, so we stay on Redis" keep the label, and so does
    # "Decision: no consensus on Kafka": content when unsure. A deferral to a target of
    # more than 40 characters is read as a topic and keeps the label too, for the same
    # reason; the bound keeps the match linear. The label may be bold or dashed
    # ("**Decision:** deferred", "Decision - open").
    rf"\b(?:outcome|conclusion|resolved|decisions?)\W*"
    rf"(?:{_NO_DECISION}|none|pending|open|deferred(?: (?:to|until) [^.;,]{{0,40}}?)?)"
    rf"{_LABEL_END}|"
    r"\bdecisions?\W+(?:tbd|tbc)\b|"
    rf"\b{_NO_DECISION}",
    re.IGNORECASE,
)
# The title shapes of meeting notes and status reports (the module docstring).
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE = (
    r"\b(?:\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})\b|w(?:ee)?k\s?\d+|"
    rf"{_MONTH}\s+\d{{1,2}}\b|\d{{1,2}}\s+{_MONTH}\b)"
)
_MEETING = (
    r"\b(?:meeting|sync|status|weekly|daily|bi-?weekly|check-in|standup|stand-up|planning|"
    r"retro(?!-)|retrospective|all[- ]hands|office hours|1:1)\b"
)
# "Sync notes 2026-09-24", "Status report - week 31": a record word after the meeting
# word; alone ("Implementation notes 2026-03-04") it names a document's topic.
_RECORD = r"(?:\W+(?:notes|minutes|update|report))?"
_NOTES_TITLE = re.compile(
    rf"{_MEETING}{_RECORD}\W+{_DATE}"
    rf"|{_DATE}\W+(?:\w+\W+){{0,2}}{_MEETING}{_RECORD}\W*(?:\.\w+)?$",
    re.IGNORECASE,
)
# A running notes document: a meeting word in the title, one dated heading per meeting.
_MEETING_TITLE = re.compile(_MEETING, re.IGNORECASE)
_DAY = (
    rf"(?:\d{{4}}[-/]\d{{2}}[-/]\d{{2}}|\d{{1,2}}/\d{{1,2}}/(?:\d{{4}}|\d{{2}})|"
    rf"{_MONTH}\s+\d{{1,2}}(?:,?\s+\d{{4}})?|\d{{1,2}}\s+{_MONTH}(?:\s+\d{{4}})?)"
)
_DATE_HEADING = re.compile(rf"^\W*{_DAY}\b\W*$", re.IGNORECASE)
# A recorded meeting's title: a date with a clock time, and a transcript or notes-by mark.
_RECORDED_STAMP = re.compile(rf"\b{_DAY}\s+\d{{1,2}}[:.]\d{{2}}\b", re.IGNORECASE)
# A transcript mark is a whole part of the title ("Planner sync — Transcript & notes — ...",
# "... - Transcript"); "Incident ... - transcript export failing" names a topic.
_TRANSCRIPT_MARK = re.compile(
    # A hyphen separates only with a space beside it: "transcript-export" is one word.
    r"(?:^|[\u2013\u2014|:]|\s-)\s*transcript(?:\s*&\s*notes)?(?:\s*$|\s*[\u2013\u2014|:]|\s+-)",
    re.IGNORECASE,
)
_NOTES_BY_MARK = re.compile(r"\bnotes by\b", re.IGNORECASE)
# A transcript's segment (a clock-time heading such as "00:12:30"), the transcript itself
# ("Transcript", "Planner sync - Transcript", "Transcription ended after ...") and the
# template's own sections; not a topic that names one ("Transcript search latency").
_TRANSCRIPT_HEADING = re.compile(
    r"^\W*\d{1,2}:\d{2}(?::\d{2})?\W*$|^\W*transcript\W*$|^\W*transcription ended\b"
    r"|\s[-\u2013\u2014|:]\s*transcript\W*$"
    r"|^\W*(?:suggested\s+)?(?:next\s+steps|action\s+items|attendees)\W*$",
    re.IGNORECASE,
)
# Links and the separators between them; a separator never holds a "[", which keeps the
# match linear.
_LINK_RUN = r"(?:\[[^\]]*\]\([^)]*\)[^\w\[]*)+"
_LONG_DATE = rf"{_MONTH}\s+\d{{1,2}},\s+\d{{4}}"
# A line of the template: the invite header's labelled links ("Invited [Ana](...)",
# "Attachments [Planner sync](...)", "Meeting records [Transcript](...)",
# "Mar 4, 2026 [Ana](...)"), the meeting's date alone, the transcript's preamble,
# the footer that asks for a rating or a review of the notes.
# "Invited [Ana](...) to walk through the budget" is a topic's text.
_TEMPLATE_TEXT = re.compile(
    rf"^[^\w\[]*(?:invited|attachments|meeting\s+records|{_LONG_DATE})\s+{_LINK_RUN}$|"
    rf"^\W*{_LONG_DATE}\W*$|"
    r"^\W*(?:this editable transcript\b|please\W+rate\b[^.]{0,60}\bsurvey\b|"
    r"you should review\s+\S+'s\s+notes(?:\W*$|\s+(?:to make sure|for accuracy)\b))",
    re.IGNORECASE,
)
# A line of links alone, the attendees the header goes on with; a line with no word in it
# (an empty heading marker "##") carries nothing.
_LINKS = re.compile(rf"^[^\w\[]*{_LINK_RUN}$")
_WORD = re.compile(r"\w")
_DESIGN_TITLE = re.compile(
    r"\b(?:design|rfc|proposal|spec|specification|architecture|adr|plan|runbook|release|"
    r"upgrade|migration|incident|postmortem|changelog)\b",
    re.IGNORECASE,
)

# A heading over open questions ("Open questions", a bare "Questions"; not "Questions it
# answers"), and a deck's closing line.
_QUESTIONS_HEADING = re.compile(
    r"^\W*(?:(?:open|unresolved|outstanding|remaining)\s+)?(?:questions|q&a)\W*$"
    r"|\bquestions\s+(?:for (?:discussion|review|reviewers|the team)|to resolve|left)\b",
    re.IGNORECASE,
)
_CLOSING_QUESTION = re.compile(r"^(?:any\s+)?questions\s*\?+$", re.IGNORECASE)
# A heading that lists decisions: content whatever its lines say, and the sections under it,
# unless it says none was reached ("Decision: TBD").
_DECISIONS_HEADING = re.compile(
    r"^\W*(?:slide\s+\d+\s*:\s*)?(?:decisions?|decided|agreed|outcomes?)\b", re.IGNORECASE
)

# Where one sentence of a line ends and the next begins.
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
# A list marker, a table pipe or a quote mark opening a line, dropped before the
# line is read.
_LINE_LEAD = re.compile(r"^\s*(?:[-+>|]|\*(?!\*)|\d{1,3}[.)])\s*")
# Underscore emphasis ("__Agreed:__"), which `\b` cannot see past; snake_case stays.
_UNDERSCORES = re.compile(r"(?<!\w)_{1,3}|_{1,3}(?!\w)")
# A markdown escape: a backslash before punctuation.
_ESCAPED = re.compile(r"\\([^\w\s])")
# A table's separator row, an empty table row and a horizontal rule carry no text.
_RULE = re.compile(r"^[\s|:\-=*_]*$")


def is_noise(title: str, section_path: tuple[str, ...], text: str) -> bool:
    """Whether one section is noise: `text` under the heading path `section_path` in the
    document titled `title`."""
    headings = tuple(_plain(h) for h in section_path)
    # A document stored with no title is named by its first heading.
    name = _plain(title) if title else (headings[0] if headings else "")
    dated = bool(_NOTES_TITLE.search(name)) or (
        bool(_MEETING_TITLE.search(name))
        and bool(_DATE_HEADING.match(_top_heading(name, headings)))
    )
    # A transcript names a recording whatever else the title says; "notes by" also signs a
    # person's notes, so it needs a meeting word and no design-document word.
    design = bool(_DESIGN_TITLE.search(name))
    recorded = bool(_RECORDED_STAMP.search(name)) and bool(
        _TRANSCRIPT_MARK.search(name)
        or (_NOTES_BY_MARK.search(name) and _MEETING_TITLE.search(name) and not design)
    )
    notes = recorded or (dated and not design)
    prose, code = _lines(text)
    # A heading is not in the section's text: a decision it states counts the same.
    # A heading that says no decision was reached is not one, in any document.
    if any(_decides(line, notes) for line in prose) or any(_decides(h, True) for h in headings):
        return False
    if any(_DECISIONS_HEADING.match(h) and not _UNDECIDED.search(h) for h in headings):
        return False
    if not prose and not code:
        # A heading with no body under it (a deck's title slide) has nothing to hide.
        return False
    if _unanswered(headings, prose, code):
        return True
    if recorded:
        # The title's own heading names the recording, not a part of it.
        own = _below_title(name, headings)
        # The template fills its section: a summary after the meeting's date is content,
        # and so is a list of links with no template line.
        if any(_TRANSCRIPT_HEADING.search(h) for h in own):
            return True
        return (
            not code
            and any(_TEMPLATE_TEXT.match(line) for line in prose)
            and all(
                _TEMPLATE_TEXT.match(line) or _LINKS.match(line) or not _WORD.search(line)
                for line in prose
            )
        )
    return notes


def _unanswered(section_path: tuple[str, ...], prose: list[str], code: int) -> bool:
    """An "Open questions" list with no answer, or a deck's closing "Questions?"; a
    design section that lists the questions a system answers is not one."""
    if code or not prose or not all(line.endswith("?") for line in prose):
        return False
    heading = section_path[-1] if section_path else ""
    return bool(_QUESTIONS_HEADING.search(heading) or _CLOSING_QUESTION.match(" ".join(prose)))


def _top_heading(name: str, section_path: tuple[str, ...]) -> str:
    """The section's top heading below the document's own title (a first heading that
    repeats the title is the title): a running notes page dates each meeting there."""
    path = _below_title(name, section_path)
    return path[0] if path else ""


def _below_title(name: str, section_path: tuple[str, ...]) -> tuple[str, ...]:
    """The headings below a first heading that repeats the document's name."""
    if section_path and _same(section_path[0], name):
        return section_path[1:]
    return section_path


def _same(heading: str, name: str) -> bool:
    # Words only: an export may turn the title's hyphens into dashes.
    return re.findall(r"\w+", heading.casefold()) == re.findall(r"\w+", name.casefold())


def _decides(line: str, notes: bool) -> bool:
    # Sentence by sentence: a question records no decision ("Are we going with Redis?"),
    # and in notes "no decision" is none; "agreed: X; Y is not decided" still is one.
    return any(
        not sentence.endswith("?")
        and _DECISION.search(_UNDECIDED.sub("", sentence) if notes else sentence)
        for sentence in _SENTENCE_END.split(line.strip())
    )


def _lines(text: str) -> tuple[list[str], int]:
    """The section's prose lines, without list markers and rules, and how many lines of
    fenced code it holds."""
    prose: list[str] = []
    code = 0
    fence = ""
    for raw in text.splitlines():
        if fence:
            if closes_fence(raw, fence):
                fence = ""
            else:
                code += 1
            continue
        if marker := fence_marker(raw):
            fence = marker
            continue
        line = _plain(_LINE_LEAD.sub("", raw)).strip()
        if line and not _RULE.match(line):
            prose.append(line)
    return prose, code


def _plain(text: str) -> str:
    """`text` with markdown escapes undone ("Planner sync \\- Transcript"), curly apostrophes
    made straight and underscore emphasis dropped."""
    return _UNDERSCORES.sub("", _ESCAPED.sub(r"\1", text).replace("\u2019", "'"))
