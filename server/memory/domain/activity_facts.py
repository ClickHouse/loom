"""Activity narration is not a memory.

An extracted fact whose sentence is an actor performing a tool action —
"User read lines 1001-2000 from MergeTreeData.cpp.", "Claude ran a command on
the files …", "Alex searched for occurrences of X and found matches in 7
files." — records what a session DID, not anything a later session should
know. The extractor can write such rows from both capture paths (the
SessionEnd transcript and the observe replay), and they rank in recall for
unrelated questions. A fact of that shape is dropped unless it carries a
durable outcome: a fix, a cause, a decision, a regression, a duplicate, a
rule. The check is deterministic — the prompt says "never User" and the
model can still write "User".

`subject_to_agent` is the attribution half: in a trajectory replay every turn
is the agent's action, so a kept fact whose subject is "User" names the agent.
"""

from __future__ import annotations

import re
from typing import Any

# The sentence's actor: the user, the assistant, a coding agent, "I", or one
# or two capitalised name words ("Alex", "Alex Kim", "Claude Code").
_ACTOR = (
    r"(?:the\s+)?(?:user|assistant|agent|operator|i|we|"
    r"claude(?:[ -]code)?|codex|cursor|"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
)
# Tool activity: reading, running, searching, listing, opening, fetching…
# Past tense, "has/have (now)" + participle, and "is/was <verb>ing".
_VERB = (
    r"(?:read|re-?read|opened|viewed|listed|ran|executed|searched|grepped|"
    r"looked\s+(?:at|up|for|through|into)|checked|inspected|retrieved|fetched|"
    r"accessed|called|invoked|queried|navigated|explored|browsed|scanned|"
    r"loaded|printed|displayed|cat(?:'?ed)?|edited|modified|opened|quoted|"
    r"requested\s+(?:a\s+)?(?:summary|the\s+contents?)|summari[sz]ed|"
    r"encountered\s+an?\s+error|reported\s+the\s+results?\s+of|"
    r"used\s+the\s+\S+\s+(?:tool|command)|"
    # The session's own tool calls and the assistant's report
    # of them ("User used loom_code_grep for …", "Assistant reported …").
    r"used\s+`?(?:loom_|mcp__|code\.|tracker\.|memory\.|knowledge\.|working_memory)\S*|"
    r"called\s+`?\S*(?:loom_|code\.|tracker\.|memory\.|knowledge\.|task\.|inbox\.)\S*|"
    r"attempted\s+to|tried\s+to|"
    r"(?:stored|saved|created|appended|registered|posted|added)\s+(?:a|an|the)\s+"
    r"(?:document|note|task|conversation|memor(?:y|ies)|key|block|value|entry|inbox|record|thread|doc)\b|"
    r"is\s+(?:reading|running|searching|checking|testing|looking|grepping|listing)|"
    r"was\s+(?:reading|running|searching|checking|looking|grepping|listing))"
)
_VERB_AFTER_NAME_RE = re.compile(
    rf"^(?:'s\s+\w+)?\s+(?:has\s+|have\s+|had\s+)?(?:now\s+|just\s+|also\s+)?{_VERB}\b",
    re.IGNORECASE,
)
_NARRATION_RE = re.compile(
    rf"^\W*{_ACTOR}(?:'s\s+\w+)?\s+(?:has\s+|have\s+|had\s+)?(?:now\s+|just\s+|also\s+)?{_VERB}\b",
    re.IGNORECASE,
)
# The actor alternatives above are case-insensitive, but the bare name form
# must really be capitalised — "the file …" is not an actor. Checked apart.
_BARE_NAME_RE = re.compile(r"^\W*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s")
_KNOWN_ACTORS = {
    "user",
    "the user",
    "assistant",
    "the assistant",
    "agent",
    "the agent",
    "operator",
    "the operator",
    "i",
    "we",
    "claude",
    "claude code",
    "claude-code",
    "codex",
    "cursor",
}
# A durable outcome: what makes a session's action worth remembering.
_OUTCOME_RE = re.compile(
    r"\b(?:fix(?:e[sd])?|fixing|root\s+cause|caused\s+by|causes?\b|because|due\s+to|"
    r"so\s+that|decid(?:e[sd]?|ing)|chose|agreed|regress(?:ed|ion)?|introduc(?:e[sd]|ing)|"
    r"broke|breaks|resolv(?:e[sd]?|ing)|workaround|duplicate\s+of|same\s+defect|"
    r"backport(?:ed)?|deprecat(?:e[sd]?|ion)|must|should|never|always|prefer(?:s|red)?|"
    r"requires?|is\s+the\s+cause|first\s+bad|bisect(?:ed)?\s+to|confirmed\s+that|"
    r"learned|turns?\s+out)\b",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().strip('"').strip()
    if isinstance(value, dict):
        for k in ("text", "value", "fact", "content"):
            v = value.get(k)
            if isinstance(v, str):
                return v.strip().strip('"').strip()
    return ""


def _actor_ok(text: str) -> bool:
    m = _BARE_NAME_RE.match(text)
    if not m:
        return True  # "User …" / "The assistant …" forms: the regex decided
    word = m.group(1)
    if word.lower() in _KNOWN_ACTORS or word.lower().startswith(("the ", "claude")):
        return True
    # A bare capitalised word is an actor only when it is a name, not the
    # first word of an ordinary sentence ("Tests ran …", "File read …").
    first = word.split()[0].lower()
    if first in _NOT_NAMES:
        return False
    # _NARRATION_RE is case-insensitive, so its two-word name
    # form also matches "Access is" + "checked" ("Access is checked per
    # caller…", a design statement). The verb must follow the capitalised
    # name itself.
    return _VERB_AFTER_NAME_RE.match(text[m.end(1) :]) is not None


# Sentence-initial capitalised words that are not people or agents.
_NOT_NAMES = frozenset(
    {
        "tests",
        "test",
        "file",
        "files",
        "the",
        "this",
        "that",
        "it",
        "ci",
        "build",
        "server",
        "loom",
        "clickhouse",
        "query",
        "queries",
        "command",
        "commands",
        "merges",
        "inserts",
        "search",
        "grep",
        "bisect",
        "code",
        "pr",
        "issue",
        "a",
        "an",
        "each",
        "every",
        "all",
        "some",
        "one",
        "two",
        "no",
    }
)


# A tool or its result as the sentence's subject — "the search
# returned …", "append added step 5b done", "knowledge_get returned the full
# text", "both appends came back stored=false", "the server rated this search
# high-risk". The session's tool output, not a fact about the user's world.
_TOOL_NOUN = (
    r"(?:search(?:es)?|server|tool|call|calls|results?|responses?|output|history|hits?|rows?|"
    r"memories|append(?:s)?|set|replace|get|list|checks?|"
    r"(?:loom_|knowledge_|memory_|working_memory_|conversation_|profile_|task_|inbox_)\w+|"
    r"(?:code|tracker|memory|knowledge|task|inbox)\.\w+)"
)
_TOOL_SUBJECT_RE = re.compile(
    r"^\W*+(?:the\s+|both\s+|all\s+\w+\s+|each\s+)?(?:[\w-]+\s+){0,3}" + _TOOL_NOUN +
    # "the raw response for loom_memory_set", "the memories returned by loom"
    # "about <topic>" up to the verb: a topic ending in a non-space, or blanks
    # only — each blank is read once, not once per way to split a run of them
    r"(?:\s+(?:for|of|from|returned\s+by|by)\s+\S+(?:\s+about(?:\s[^,.;]*?[^\s,.;]|\s\s(?=\s)))?)?\s+"
    r"(?:returned|found|shows?|showed|stored|saved|added|made|swapped|rated|ranked|came\s+back|"
    r"answered|refused|failed|matched|ran|succeeded|worked|includes?|contains?|lists?|reads|says)\b"
    # "the result of loom_memory_update is …": a named tool's result described
    r"|^\W*+(?:the\s+)?(?:raw\s+)?(?:results?|responses?|output)\s+(?:of|for|from)\s+`?"
    r"(?:loom_|knowledge_|memory_|working_memory_|code\.|tracker\.|memory\.)\S*\s+(?:is|was|were|are)\b"
    # "the loom_memory_history has 25 versions": counts only after a tool name
    r"|^\W*+(?:the\s+)?(?:loom_|knowledge_|memory_|working_memory_)\w+\s+(?:has|had)\s+\d+"
    # "the memories returned by loom about X are about Y": a result described
    r"|^\W*+(?:the\s+)?(?:memories|results?|hits?|rows?)\s+returned\s+by\b",
    re.IGNORECASE,
)
# What still makes a tool's result worth keeping: a defect and its cause.
# (The wider _OUTCOME_RE keeps "due to lack of entities" — a tool's own
# status line, not an outcome.)
_STRONG_OUTCOME_RE = re.compile(
    r"\b(?:fix(?:e[sd])?|root\s+cause|caused\s+by|because|regress(?:ed|ion)?|bug|"
    r"broke|duplicate\s+of|wrong)\b",
    re.IGNORECASE,
)
# "User's attempt to register an inbox was refused …", "the task sequence
# could not run …": the report of a failed call, whoever the subject is.
# The assistant reporting on its own work ("Assistant reported that the plugin
# server defaulted to …", "Assistant stated that neither principal can read
# …"): a statement about the session. A USER reporting a symptom is a fact.
_ASSISTANT_REPORT_RE = re.compile(
    r"^\W*(?:the\s+)?(?:assistant|agent|claude(?:[ -]code)?|codex)\s+"
    r"(?:reported|stated|noted|said|confirmed)\b",
    re.IGNORECASE,
)
_FAILED_ATTEMPT_RE = re.compile(
    r"\b(?:attempt|call|request|sequence|registration)\b[^.;]*?"
    r"\b(?:refused|could\s+not|couldn't|failed|was\s+rejected)\b",
    re.IGNORECASE,
)
# The report of a refused or failed call: never a durable outcome, even when
# it says why ("refused due to access issues").
_CALL_FAILURE_RE = re.compile(
    r"\b(?:refused|was\s+rejected|failed|could\s+not|couldn't|was\s+not\s+(?:saved|stored)|"
    r"stored=false|not\s+in\s+your\s+accessible|role\s+check|permission|[45]\d\d\s+[A-Z_]{4,})\b",
    re.IGNORECASE,
)


def is_activity_narration(value: Any) -> bool:
    """True for "<actor> <tool activity> …" with no durable outcome, and for
    a tool or its result reporting what it did."""
    text = _text(value)
    if not text:
        return False
    prose = _PATHLIKE_RE.sub(" ", text)
    if _TOOL_SUBJECT_RE.match(text):
        return _STRONG_OUTCOME_RE.search(prose) is None
    if _FAILED_ATTEMPT_RE.search(text) and _STRONG_OUTCOME_RE.search(prose) is None:
        return True
    if _ASSISTANT_REPORT_RE.match(text) and _STRONG_OUTCOME_RE.search(prose) is None:
        return True
    if not _NARRATION_RE.match(text) or not _actor_ok(text):
        return False
    if _CALL_FAILURE_RE.search(text):
        return True
    # An outcome word inside a path or identifier ("a-pinned-row-is-never-
    # evicted.md", `fix_retry`) is not an outcome of the action.
    return _OUTCOME_RE.search(prose) is None


# A token with a path separator, a dotted/hyphenated/underscored identifier,
# or anything in backticks. A token is read from its start or after a
# backtick, over at most 255 characters each side: the search
# then costs one pass over the text, not one per character of a long token.
_PATHLIKE_RE = re.compile(
    r"`[^`]*`|(?<![^\s`])(?:\S{0,255}[/\\]\S{0,255}|\S{1,255}[._-]\S{0,255}[._-]\S{0,255}"
    r"|\S{1,255}\.\w{1,5}\b)"
)


_USER_SUBJECT_RE = re.compile(r"^(\W*)(?:the\s+)?user\b(?!'s)", re.IGNORECASE)


def subject_to_agent(value: Any, agent: str) -> Any:
    """In a trajectory replay the subject is the agent: "User ran X" → "<agent>
    ran X". A possessive ("User's laptop") names the person and is kept."""
    name = (agent or "").strip()
    if not name or name in ("*", "-"):
        return value
    if isinstance(value, str):
        return _USER_SUBJECT_RE.sub(lambda m: f"{m.group(1)}{name}", value, count=1)
    if isinstance(value, dict):
        for k in ("text", "value", "fact", "content"):
            if isinstance(value.get(k), str):
                return {**value, k: subject_to_agent(value[k], name)}
    return value


def partition(facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped) — dropped are activity narration."""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for f in facts:
        (dropped if is_activity_narration(f.get("value")) else kept).append(f)
    return kept, dropped
