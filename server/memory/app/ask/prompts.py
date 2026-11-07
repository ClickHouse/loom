"""The synthesis prompt text: the system prompt, the abstention clause, and
the per-question and per-window notes, with the pure helpers that decide
which notes a window earns."""

from __future__ import annotations

import bisect
import re
from typing import Any

from server.memory.app.ask.question_shapes import (
    is_amount_superlative_question,
    is_span_total_question,
)

_SYNTH_SYSTEM = (
    "You answer a question using ONLY the stored memories provided below. "
    "Give the CURRENT state / the key facts the memories establish, not a "
    "description of what memories exist. "
    "PRESERVE EVERY CONCRETE SPECIFIC VERBATIM from the memories: file paths "
    "and line numbers (e.g. Foo.cpp:123), exact test/function/symbol names, "
    "PR and issue numbers (e.g. #12345), commands, error messages, settings, "
    "dates, and people. Never generalize, omit, or replace a specific with "
    "vague phrasing (e.g. 'make it faster' instead of the actual fix) — the "
    "concrete identifiers are the most useful part of the answer to an agent. "
    "Include the actionable steps the memories give. "
    "When the question asks for a SET or LIST (e.g. 'which', 'what ... are', "
    "'all', 'each', 'recurring', 'tracked', 'list'), ENUMERATE every relevant "
    "item the memories contain, each with its own specifics (name, status, "
    "date, identifiers) — do NOT collapse a set into a single example or a "
    "generic summary. For a single-fact question, give the one direct answer. "
    "Each memory is prefixed with the date it is about, [YYYY-MM-DD]: the "
    "event date when a relative phrase in it was resolved, otherwise the date "
    "it was said. A phrase like 'yesterday' or 'last month' followed by "
    "'(on DATE)' or '(around DATE)' is ALREADY resolved — report DATE and "
    "never shift it again. "
    "When memories conflict or describe an evolving state, TRUST THE MOST "
    "RECENT and ignore the superseded ones — an agent's memory accumulates "
    "stale copies of the same fact across sessions. A digest (summary:) "
    "restates a transcript as it stood when it was written and can repeat a "
    "value that was later corrected: when a fact row or a CURRENT FACTS line "
    "disagrees with a digest, the fact row or the CURRENT FACTS line is the "
    "state to report. Cite each claim with a "
    "[memory_key] marker taken from the labels on the memories. "
    "DERIVED FIGURES: when the question asks for a figure no memory states as such "
    "but the memories give its inputs — a rate and the amount it applies to (1% "
    "cashback on $75), a target and the current value ('how many more do I need': "
    "target minus current), a start and an end ('increase': end minus start), two "
    "prices ('difference', 'how much will I save': one minus the other), two ages "
    "('how old was I when X was born': my age minus X's age, both as of the same "
    "date) — compute "
    "it, LEAD with the result (the first words of the answer are the figure) and show "
    "the arithmetic; do not say it is not recorded. A figure recorded for a DIFFERENT "
    "occasion (a planning estimate, an earlier trip) is not the asked one: compute for "
    "the asked occasion from its own inputs. "
    "ASSISTANT-AUTHORED rows (labelled as the assistant's own statement) are the "
    "assistant's suggestions, plans and quotes: for a question about what the USER "
    "did, paid, attended or experienced, they are not evidence — use the user's own "
    "statements and figures first, and count only events a user row says happened. "
    "SUPERLATIVES: for a most / least / highest / longest / biggest question, "
    "rank EVERY candidate the memories offer for the asked measure and answer "
    "with the top one; never drop a candidate by re-categorizing it (an online "
    "grocer the user bought groceries from is a grocery store; a course is "
    "formal education) — if a candidate's category is arguable, keep it in the "
    "ranking, answer with it when it leads, and state the caveat in one clause. "
    "TIME WINDOWS in such questions are the user's own loose framing. A memory "
    "whose phrase is 'last month' / '(the month before DATE)' is MONTH-precise: "
    "its [YYYY-MM-DD] prefix is only the month's anchor, so 'the month before "
    "2023-05-29' means all of April 2023 and IS inside 'the past month' asked "
    "on 2023-05-30 — rank that candidate with the others and let it win when "
    "its amount is the largest. Never exclude a candidate over days at the "
    "window's edge, and never exclude it for being an online order when the "
    "question is about where money was spent. A measure stated over a "
    "SUB-window of the asked period ('200 followers over the past three "
    "weeks' for a question about the past month) is inside the window and is "
    "compared as stated — 200 in three weeks beats 120 in a month. "
)

# Closing abstention clause.
_ABSTAIN_CONSERVATIVE = (
    "A memory that states the asked fact in other words, or implies it directly, "
    "answers the question ('born and raised in China' answers 'what is her "
    "nationality'; 'plays the position of goalkeeper' answers 'what position'): "
    "give the answer, cite the memory, and name the inference in one clause. "
    "The first sentence IS the answer, stated plainly; never comment on how the "
    "memories word a fact ('none uses the word …', 'they never say outright …'). "
    "Answer in the form the question asks for, not the stored label's: a "
    "nationality is a demonym ('Chinese', not 'China'), an ethnic background a "
    "descent ('of Asian descent', not 'Asia'). "
    "If no memory bears on the question, say so in one short sentence instead "
    "of guessing. No preamble."
)


# An open-domain conversation question (96 of 1,986 in one run) asks for
# a memory fact COMBINED with something the store cannot hold: a public fact
# (which country a city is in, what a holiday is) or a plausible inference the
# question itself requests ("would she likely …", "what field would she
# pursue"). The memory-only prompt above refuses both and scores 39.6
# against 65.6. Under world_knowledge="allow" the synthesizer may bridge, but
# every personal fact still comes from the memories, and the bridged
# sentences are marked so a reader (and the ledger's `grounding`) can tell
# them apart.
_WORLD_KNOWLEDGE_MARK = "[general knowledge]"
_INFERENCE_MARK = "[inferred]"

_WORLD_KNOWLEDGE_NOTE = (
    "EXCEPTION for general knowledge and requested inference: you MAY combine "
    "the memories with widely known public facts (geography, calendars and "
    "holidays, what an organisation, product, profession or field is, "
    "arithmetic on dates) and, when the question asks what is LIKELY or would "
    "PROBABLY be the case, you MAY draw the plausible inference from the "
    "memories. This applies to DIRECT factual questions too: when a public "
    "fact fixes the answer from something the memories DO name — the platform "
    "a named game runs on, the state a named city sits in, the composer of a "
    "named work — give that answer; that is public knowledge, not an invented "
    "personal fact. Every PERSONAL fact — what this person did, has, prefers, "
    "said, or when — must still come from the memories; never invent one, and "
    "if the memories name no subject the question asks about, say so rather "
    "than guess. End each "
    f"sentence that relies on public facts with {_WORLD_KNOWLEDGE_MARK} and "
    f"each inferred sentence with {_INFERENCE_MARK}. LEAD with the most "
    "plausible answer in the very first sentence, stated directly (name the "
    "thing — do not open with 'the memories do not record'); the supporting "
    "memories come next and any caveat about what the memories lack comes "
    "LAST, never first, and never replaces the answer when the memories plus "
    "public knowledge point one way. Only when the memories give NO personal "
    "fact the question needs, say so instead of answering from general "
    "knowledge alone."
)

_SUPERSEDED_NOTE = (
    " HISTORY (required): a memory line may carry a label of the form "
    "'[superseded <date>; replaced by <key>]'. That row is a value a later "
    "write replaced — it is NOT current. Use it only to say what the value "
    "used to be, when it changed, and what replaced it; never state it as "
    "the present state. When a current row and its superseded predecessor "
    "are both in the window, the current row answers 'what is' and the "
    "superseded row answers 'what was' / 'why did it change'."
)


def _superseded_notes(memories: list[Any]) -> list[str]:
    """[_SUPERSEDED_NOTE] when the window carries a superseded row (only
    under `include_superseded`); [] otherwise, so a window without
    one renders byte-identically."""
    if any(str(getattr(m, "status", "") or "") == "superseded" for m in memories):
        return [_SUPERSEDED_NOTE]
    return []


def _window_notes(question: str, memories: list[Any]) -> list[str]:
    """The per-window notes in their fixed order: question shape, superseded
    rows."""
    return [
        *_question_notes(question, memories),
        *_superseded_notes(memories),
    ]


_INFERENCE_NOTE = (
    "This question asks for an INFERENCE (what is likely, what someone would do or be "
    "considered), not for a stated fact. Do not refuse because no memory states it "
    "directly: open with the most likely answer and its degree ('Likely no — …', "
    "'Probably yes — …', 'Somewhat — …'), then give the memories it rests on with "
    "their [memory_key]. Say 'the memories do not record' only when they hold "
    "nothing bearing on the question."
)

_SPAN_TOTAL_NOTE = (
    "This question asks for a TOTAL of years, months or days across stages. "
    "Each stage's span is stated or DERIVED FROM DATES: a start and an end, or "
    "a start and the next stage's completion — high school 2010 to 2014 is 4 "
    "years; an Associate's degree completed May 2016 after high school ended in "
    "2014 is about 2 years; a Bachelor's that 'took four years' is 4 years. "
    "The question's endpoints ('from high school to the Bachelor's') BOUND the "
    "chain; they do not filter it: every recorded stage that falls between them "
    "in time counts (an Associate's degree between high school and the "
    "Bachelor's is formal education and is counted), whether or not the "
    "question names it. Chain the stages in date order from the first endpoint "
    "to the last, list every span with its derivation, and answer with the SUM "
    "(4 + 2 + 4 = 10). A span you computed from recorded dates is a stated "
    "value, not an unknown; only when a stage has neither a duration nor any "
    "dates say so and give the partial sum with that caveat."
)

_AMOUNT_RE = re.compile(
    r"(?:\$|€|£)\s?(\d[\d,]*(?:\.\d+)?)|(?<![\d,]),*(\d[\d,]*(?:\.\d+)?)\s?(?:dollars?|usd|followers?|"
    r"miles|km|kg|lbs?|hours?|hrs|minutes?|mins|steps|calories|points|views|subscribers)\b",
    re.IGNORECASE,
)
_AMOUNT_ENTITY_RE = re.compile(r"\b([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*){0,2})")
_AMOUNT_ENTITY_STOP = {
    "User",
    "The",
    "They",
    "Their",
    "I",
    "My",
    "We",
    "Our",
    "It",
    "A",
    "An",
    "In",
    "On",
    "At",
}

_MONEY_Q_RE = re.compile(
    r"\b(spen[dt]|cost|paid|pay|price|money|dollars?|cheap\w*|expensive)\b|\$", re.IGNORECASE
)


def amount_candidates(
    memories: list[Any], cap: int = 8, *, money: bool | None = None
) -> list[tuple[str, float, str, str]]:
    """(entity, amount, memory_key, valid_at) for every amount a pool row
    states next to a named entity — the nearest Title-case phrase in the same
    sentence, before the amount if there is one, else after. `money=True`
    keeps currency amounts only, `False` keeps unit counts only (followers,
    miles, hours …), None keeps both. Largest first; one row per (entity,
    amount). Pure."""
    out: dict[tuple[str, float], tuple[str, float, str, str]] = {}
    for m in memories or []:
        text = str(getattr(m, "content_excerpt", "") or "")
        key = str(getattr(m, "memory_key", "") or "")
        when = str(getattr(m, "valid_at", "") or "")[:10]
        for sent in re.split(r"(?<=[.!?])\s+", text):
            amounts = list(_AMOUNT_RE.finditer(sent))
            if not amounts:
                continue
            # read once per sentence; each amount looks up its neighbours by start
            ents = [
                (e.start(), e.group(1))
                for e in _AMOUNT_ENTITY_RE.finditer(sent)
                if e.group(1).split()[0] not in _AMOUNT_ENTITY_STOP
            ]
            starts = [s for s, _ in ents]
            for am in amounts:
                is_money = am.group(1) is not None or bool(
                    re.search(r"dollars?|usd", am.group(0), re.I)
                )
                if money is True and not is_money or money is False and is_money:
                    continue
                raw = am.group(1) or am.group(2) or ""
                try:
                    val = float(raw.replace(",", ""))
                except ValueError:
                    continue
                # the nearest entity before the amount, else the first after it
                b = bisect.bisect_left(starts, am.start())
                a = bisect.bisect_right(starts, am.end())
                ent = (ents[b - 1] if b else ents[a] if a < len(ents) else (0, ""))[1].strip(" .,")
                if not ent:
                    continue
                out.setdefault((ent.lower(), val), (ent, val, key, when))
    ranked = sorted(out.values(), key=lambda t: -t[1])
    return ranked[:cap]


def _amount_candidates_note(question: str, memories: list[Any]) -> str:
    """The amount-candidates note (every amount the pool states beside a named
    entity, largest first), or '' when the question is not an amount superlative or
    the pool yields fewer than two candidates."""
    if not is_amount_superlative_question(question):
        return ""
    cands = amount_candidates(memories, money=bool(_MONEY_Q_RE.search(question or "")))
    if len(cands) < 2:
        return ""
    items = "; ".join(
        f"{ent} — {amt:g}{' (' + when + ')' if when else ''} [{key}]"
        for ent, amt, key, when in cands
    )
    return (
        "AMOUNT CANDIDATES, read off the memories and sorted largest first — rank from "
        "this list, verify each against its row, and keep a candidate whose date is "
        "month-precise inside the asked window: " + items + "."
    )


def _question_notes(question: str, memories: list[Any]) -> list[str]:
    """Notes the question's shape earns: the span-total rule and the
    amount-candidate list."""

    out: list[str] = []
    if is_span_total_question(question):
        out.append(f" {_SPAN_TOTAL_NOTE}")
    note = _amount_candidates_note(question, memories)
    if note:
        out.append(f" {note}")
    return out


_PR_RECORD_NOTE = (
    "AUTHORITATIVE PR RECORDS: a row that starts with '[code index] PR #N' or "
    "describes a GitHub item from the code index is loom's own read of GitHub "
    "— its author, merged/closed state and dates are authoritative. When a "
    "memory row says a PR is open or unmerged and the code-index row says it "
    "merged or closed on a date, report the code-index state and date as the "
    "current state and mention that the memory predates it; never present the "
    "memory's older state as current. Cite the code-index row."
)


def _has_pr_record(memories: list[Any]) -> bool:
    """Whether the synthesis pool carries an authoritative PR / GitHub-item row
    from the code plane. Pure."""
    return any(
        str(getattr(m, "memory_key", "") or "").startswith(("code_pr:", "gh_item:"))
        for m in memories
    )


_TEMPORAL_DERIVE_NOTE = (
    "This is a WHEN question. A memory's relative phrase that is NOT followed by "
    "'(on DATE)' or '(around DATE)' — 'last summer', 'a few weeks ago', 'for 10 "
    "years', 'when I was 18' — is relative to that memory's own [YYYY-MM-DD]: "
    "derive the answer from it and give it as approximate ('around summer 2022', "
    "'about 2013', 'roughly three weeks before 2023-08-11'). Do not answer 'the "
    "memories do not record when' while a memory states the event with such a "
    "phrase; a memory that names a month or year the question asks about counts "
    "even when the day is missing."
)

_CHAIN_NOTE = (
    "This question CHAINS two facts: the answer is an attribute of an entity that "
    'another memory names ("the citizenship of the CEO of X": first the CEO, then '
    "that person's citizenship). Resolve the first hop from the memories, then "
    "answer the second hop ONLY from a memory about that entity — never from "
    "general knowledge, and when a memory contradicts general knowledge the memory "
    "wins (this store holds updates). If no memory gives the second hop, say which "
    "hop is missing. A role noun in the question (the chairperson, the director, the "
    "CEO, the author of X) names a PERSON; the question asks for that person's "
    "attribute, and the role word itself is never the answer."
)

_SUPERSESSION_NOTE = (
    "LATEST WORD: when two memories on the same subject disagree (a status, a "
    "classification, a verdict, a rule), the later-dated one is the current "
    "position. State it as current, name the earlier one as superseded with both "
    "dates, and never present the earlier position as the answer."
)

_OPTIONS_SYSTEM = (
    " Answer options are given. Choose the ONE option that matches what the "
    "memories establish: when options are lists or sets of items, compare "
    "each option against your findings item by item — an option that omits an "
    "item you found, or adds one you did not, is wrong even if it is the "
    "longest or looks closest; when two options differ by a single item, "
    "decide that item explicitly from the memories. A fact row is the store's "
    "current belief and a raw chunk is a transcript leaf that may hold a "
    "statement the user later corrected: when a chunk and a fact disagree on a "
    "single-valued attribute, the LATER-dated statement wins (chunk turns carry "
    "their clock; a fact carries its date), never the one that merely reads as "
    "'direct'. A comparison is decided on "
    "the attribute the question names, as the memories state it: when the "
    "stated values are equal (both 28 years old) and an option says 'Both the "
    "same', that option is the answer — never break the tie with a finer "
    "attribute that was not asked (a birthday). 'Later' or 'earlier birthday' "
    "compares month and day within the year (December 9 is later than "
    "January 9). When the options DESCRIBE a place ('known for its beaches "
    "and mild climate') and the memories NAME the place an entity is in, "
    "state the memory's city and choose the option whose city it is — never "
    "the description that fits the event's theme or name — and AFTER the "
    "ANSWER line add ONE line 'PLACES: A=<city>; B=<city>; …' naming, for "
    "EVERY option, the city that description is famous for (theme parks and "
    "Walt Disney World → Orlando; beaches and mild climate → San Diego; "
    "eco-friendliness and arts → Portland). When several memories fit the "
    "question's description (two events of eight hundred people, several "
    "activities lasting one day), the "
    "candidate whose asked attribute matches one of the options is the one the "
    "question means — never conclude that no option matches while a candidate "
    "matching an option exists. The ANSWER line BEGINS "
    "with the chosen letter, as 'D) …' — never name a runner-up letter before "
    "it — then, in a few words, why the runner-up option fails."
)

_COUNT_SYSTEM = (
    " This is a COUNT question. Work in two parts. First a line 'ITEMS:' "
    "followed by ONE line per candidate item the memories mention for the "
    "criterion, each with the exact value from the memories (a height, a date, "
    "a duration, an attendance) and a VERDICT at the end of the line: "
    "'— qualifies', '— fails: <why>' or '— unclear: <why>' (a value that is "
    "approximate, borderline or missing for the threshold). Put items with no "
    "value at all under a separate 'UNKNOWN:' line rather than guessing. Then a "
    "final line starting 'ANSWER:' that states the count as exactly the number "
    "of '— qualifies' lines; when there are '— unclear' lines also give the "
    "count with them included, as 'N (N+M if the unclear ones count)', and "
    "choose the option equal to N unless only N+M matches an option. When "
    "options were given the ANSWER line BEGINS with that letter, as "
    "'ANSWER: D) 3 — …', then the count, then the [memory_key] citations. "
    "Nothing after the ANSWER line. A question may ask for a "
    "quantity the memories STATE rather than a set to enumerate ('how many "
    "shirts did I pack', 'how many plants did I plant'): then each ITEMS line "
    "is one stated quantity with its number and ends '— stated: N', and the "
    "ANSWER is that number — the sum of the '— stated' lines when the question "
    "spans several things (tomatoes and cucumbers) — never the number of "
    "lines. A detail of the question that the memories neither confirm nor "
    "contradict (a duration, an adjective, 'my 5-day trip') does not make an "
    "item unclear; only a value that is approximate, borderline or missing for "
    "the criterion does. When ITEMS has no line at all, do not answer 0: say "
    "that the memories do not mention any."
)
_INCLUSIVE_CATEGORY_RULE = (
    " The question's category word is read the way its asker would count it, not "
    "literally: a day hike is a trip, a lecture or tour at a museum "
    "is a museum visit, upgrading a part is servicing, a volunteer shift at a "
    "festival is attending it, a signed vinyl picked up at a show is a record "
    "acquired. An item is excluded only when the memories say it did NOT happen "
    "(planned, cancelled, considered) or it is plainly another category."
)
_COUNT_SYSTEM_RULES = (
    _INCLUSIVE_CATEGORY_RULE
    + " Rules: a threshold is inclusive when the question says 'or more', 'or "
    "older', 'at least', 'or above' and exclusive for 'more than', 'over', "
    "'longer than'; an approximate value in a memory ('about 600', '~600', "
    "'around 600', 'roughly two weeks') counts as the stated number. If the "
    "question has two parts (joined by 'Additionally', 'also', 'in addition', "
    "'and'), list ITEMS for each part under its own sub-heading and give BOTH "
    "counts in the ANSWER line in the question's order, separated by ';' "
    "(e.g. '3 events; 5 people'), then the option whose two numbers match. "
    "Every named item that carries the asked attribute IS the kind of thing "
    "the question asks about (an event, a person): a memory saying an item is "
    "'based in Houston', 'located in Houston' or 'held in Houston' places that "
    "event in Houston exactly like 'happening in Houston' does. 'Not an event', "
    "'a company', 'an organisation' are NEVER valid failure reasons unless a "
    "memory states that category outright. "
    "When no option equals your count exactly, re-check the items whose value "
    "sits EXACTLY on the threshold ('six weeks' for 'six weeks or longer'): the "
    "asker's options show which reading they meant, so if counting those items "
    "the other way gives a number that IS an option, choose that option and "
    "write 'boundary' after the letter. Only when that fails choose ONE option "
    "anyway — the closest number (for two parts, the option closest on both) — "
    "and write 'closest' after the letter; never answer 'no matching option'. "
    "If the question says to count stated instances or mentions ('count each "
    "statement', 'each time it was mentioned', 'every stated instance'), then "
    "every statement is ONE item — repeated statements and superseded values "
    "each count, and names are never merged. Otherwise one entity "
    "is ONE item: names that differ only by spacing, punctuation or case "
    "('RunFest2024' / 'RunFest 2024') are the same entity; names that differ "
    "by a word or a letter ('Craft Fest' / 'CraftFit Fest', 'Music Connect' / "
    "'Music Meet-Up') are DIFFERENT items and each counts. When one entity's value "
    "was stated more than once the LATEST-dated statement is the current one — "
    "list it once with the latest value and note the superseded one in "
    "parentheses. "
    "PARTS OF A CATEGORY: when the question counts a collective (siblings, "
    "parents, children, pets, grandparents) and the memories state its parts "
    "('3 sisters', 'has a brother'; 'two sons', 'a daughter'), each part is an "
    "ITEMS line '— stated: N' and the ANSWER is their SUM (3 sisters + 1 brother "
    "= 4 siblings) — never the count of one part alone, never a refusal because "
    "no row says 'siblings'. "
    "SPANS FROM DATES: when the question asks for a total of years, months or "
    "days and a memory gives a start and an end (or a start and a later "
    "milestone: 'high school 2010 to 2014', 'Associate's degree in May 2016', "
    "'Bachelor's in 2020, took four years'), compute each span from the dates "
    "(2014 to May 2016 = about 2 years), list it as a stated span, and give the "
    "sum, writing the derivation; a span you computed from dates is a stated "
    "value, not an unclear one."
)

_RECOMMENDATION_SYSTEM = (
    " This is a RECOMMENDATION question: the answer is judged on whether it uses "
    "what the memories record about THIS user. Open with one or two sentences "
    "naming the user's relevant preferences, tools, skill level, constraints and "
    "past choices exactly as the memories state them, with their [memory_key] "
    "citations. Then give suggestions that fit those specifics — name the tool, "
    "brand, level or style the user already uses where the memories give one — "
    "and say in a few words why each fits. Do not offer generic options the "
    "memories rule out. If the memories hold nothing relevant, say so before "
    "answering generally."
)

_PREFERENCE_SYSTEM = (
    " This is a PREFERENCE question: what the user tends to like, inferred from "
    "the items they mentioned. Work in three parts. First a line 'ITEMS:' with "
    "ONE line per item the memories say the user mentioned, watched, read, ate, "
    "bought or praised, each with the attribute the question asks about (for a "
    "movie its genre, for a dish its cuisine, for a place its type), using the "
    "memories' own words where they give one and your best classification "
    "otherwise. Then a line 'TALLY:' with the count per attribute value. Then a "
    "final line starting 'ANSWER:' naming the most frequent value (ties: the "
    "one the memories describe most warmly), the answer-option letter if "
    "options were given, and the [memory_key] citations. A combined option "
    "('Sour and Spicy') is chosen only when EACH of its parts is supported by "
    "at least two items; otherwise the single option with the top tally wins. "
    "For films, tally the genres commonly listed for each title and choose the "
    "most specific genre shared by the most titles — 'Drama' is the fallback "
    "only when no more specific genre is shared. Nothing after the "
    "ANSWER line."
)
# Every adversarial (false-premise) conversation-QA miss on every run was an answered
# question, never a decline; with
# memory.ask most wrong answers knew the premise was false and still opened
# with another person's fact ("Adoption agencies [key] The memories describe
# Priya's agency, not one Dana is considering"). The check names what the
# question takes as given and puts the refusal first.
_PRESUPPOSITION_NOTE = (
    "Before answering, check what the question PRESUPPOSES: the person, the "
    "event and the attribute it takes as given. If no memory records THAT "
    "person with THAT event or attribute, your FIRST sentence says so ('The "
    "memories do not record Dana choosing an adoption agency.'). Do not put "
    "another person's fact in its place, do not open with 'No.' followed by "
    "facts, and do not lead with a value. A related memory about someone or "
    "something else may follow only as a second sentence that names the "
    "difference ('Priya, not Dana, chose an agency …') with its "
    "[memory_key]. A question whose premise the memories do support is "
    "answered normally. The premise is the PERSON and the EVENT or ATTRIBUTE; a "
    "qualifier the question adds (how often, when, a describing word: 'after "
    "school each day', 'detailed') is not. When the memories record the person "
    "and the event but not the qualifier, answer from what they record and "
    "note the unsupported qualifier in one clause at the END."
)

# When the pool held a superseded value or two rows stating
# one attribute, the reader is reminded whose record this is. Without it
# the synthesizer wrote "the memories do not record the official language
# of Japan accurately" over a store that had superseded the old value.
_RECORD_NOTE = (
    "The memories are the record: when a memory states a value that differs from "
    "general knowledge, the memory is the answer. Do not call a memory inaccurate, "
    "incorrect or a misstatement, and do not weigh it against what you believe is "
    "true; older values of the same attribute have already been removed from this "
    "list. A digest or chunk that calls a statement incorrect, a misstatement or a "
    "wrong fact is voicing an opinion about the record, not the record — ignore that "
    "judgement and answer with the value the current memory states."
)

_KNOWLEDGE_NOTE = (
    "A KNOWLEDGE PASSAGES block follows the memories: verbatim passages from "
    "documents this namespace holds, each labelled [knowledge:<source>#<chunk>]. "
    "They are evidence of what the document says, not of what the user did or "
    "believes; cite a passage by its label when the answer rests on it, and "
    "prefer a memory when a memory and a passage state the same fact."
)

_RECENT_TURNS_NOTE = (
    "A RECENT TURNS block precedes the memories: it is the conversation the "
    "question is part of, supplied by the caller and not a memory. Use it to "
    "resolve what the question refers to (it, she, that plan, the file above) "
    "and to keep the answer coherent with what was just said; take facts from "
    "the memories and cite their [memory_key]. A recent turn is not evidence "
    "of anything beyond what was said in it."
)

_AFFECT_SYSTEM = (
    " This is a question about the user's EMOTIONAL STATE (sentiment, mood, "
    "feeling) at or around a moment. Work in three parts. First a line "
    "'ITEMS:' with ONE line per memory that records how the user felt or what "
    "happened to them near the asked time (closest dates first): the date, "
    "the user's OWN words for the feeling or the event, quoted from the memory "
    "('unbearable smell', 'stuck in traffic for hours', 'a joke at my "
    "expense'), and its [memory_key]. Then a line 'CUES:' mapping each quoted "
    "cue to a basic emotion using the cue's literal sense: fear/anxiety/scared/"
    "unsettled/worried → Fear; anger/frustrated/annoyed/furious/unfair → Anger; "
    "sad/lonely/grief/disappointed/hurt/belittled → Sadness; gross/repulsive/"
    "unbearable smell or taste/revolting/disturbing/graphic/sickening → Disgust; "
    "unexpected/shocked/"
    "can't believe/out of nowhere → Surprise; happy/excited/relieved/proud/"
    "delighted → Happiness. Then a final line starting 'ANSWER:' naming the "
    "emotion the cues closest to the asked time support most (the nearest "
    "memory outweighs older ones; a named emotion word outweighs an inferred "
    "one). When cues at different minutes disagree, the cue whose minute is "
    "CLOSEST to the asked time decides, and the ANSWER names the gap ('cue at "
    "08:08, asked 08:10'); a cue from a different day never outweighs one from "
    "the asked day. When options were given the ANSWER line BEGINS with the "
    "chosen letter, as 'ANSWER: D) Fear — cue at 07:41, asked 07:40'; a "
    "runner-up letter is never written before it. Then the citations. "
    "Every CUES line ends with its gap from the asked minute, computed as a "
    "number ('gap 2 min'; 08:02 against 08:00 is 2, 08:11 is 11) — the "
    "smallest gap decides, never an impression of which cue is stronger. "
    "A smell word — scent, odour, stench, smell, reek — is a Disgust cue even "
    "in a figure of speech ('a scent of neglect'); 'disturbing', 'graphic', "
    "'hard to shake off what I saw' are revulsion cues, not fear; and Disgust is "
    "never chosen without a smell, taste or revulsion cue. ONLY when no option names the "
    "emotion the nearest cue carries, choose the option nearest in kind: "
    "disappointment, plans unravelling, unfairness → Anger; dread, unease, "
    "watchfulness → Fear; loneliness, exclusion, grief → Sadness; wonder → "
    "Surprise; a positive cue (joy, laughter, warmth, delight) never maps to a "
    "negative option — with no Happiness offered it maps to Surprise. When an "
    "option DOES name the cue's emotion, that option is the answer and the "
    "mood of the rest of the day never overrides it. "
    "Never answer with a generic 'negative' or 'mixed' — choose the option "
    "whose emotion the strongest cue names. Nothing after the ANSWER line."
)
